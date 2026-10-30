"""Fast tests for corla_observer.classify, on small synthetic files built in code.

Every kind is covered, with the header variants, encodings and layouts seen
in the audit center's files over the years. The acceptance test on real
files from several elections is tests/acceptance/test_classify_mirror.py.
"""

from __future__ import annotations

import codecs
import io
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from corla_observer.classify import (
    COLORADO_COUNTIES,
    KINDS,
    SIGNATURES,
    Classification,
    XlsxError,
    XlsxReader,
    canonical_county,
    classify_bytes,
    classify_file,
    county_from_filename,
    extract_seed,
    match_signature,
    normalize_header,
)
from corla_observer.cli import app
from corla_observer.sampling import load_county_manifest
from synthetic import SEED, make_xlsx, state_audit_report


def csv_bytes(*lines: str, encoding: str = "utf-8") -> bytes:
    return ("\r\n".join(lines) + "\r\n").encode(encoding)


def test_kinds_and_signatures_agree() -> None:
    assert len(COLORADO_COUNTIES) == 64
    assert len(set(KINDS)) == len(KINDS)
    assert {s.kind for s in SIGNATURES} <= set(KINDS)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Adams", "Adams"),
        ("BACA", "Baca"),
        ("Conejos ", "Conejos"),
        ("ClearCreek", "Clear Creek"),
        ("SanMiguel", "San Miguel"),
        ("El Paso County", "El Paso"),
        ('="La Plata"', "La Plata"),
        ("Prowes", None),
        ("Colorado", None),
        ("", None),
    ],
)
def test_canonical_county(value: str, expected: str | None) -> None:
    assert canonical_county(value) == expected


def test_normalize_header() -> None:
    assert normalize_header(" # of Ballot Cards ") == "ofballotcards"
    assert normalize_header('="Tabulator ID"') == "tabulatorid"
    assert normalize_header("Batch ") == normalize_header("batch")


def test_match_signature_reports_columns_and_needs_every_required_one() -> None:
    manifest = next(s for s in SIGNATURES if s.kind == "county_manifest")
    found = match_signature(["County", "Tabulator ID", "Batch #", "#of Ballots", "Box # Location"], manifest)
    assert found is not None
    assert found["tabulator"] == (1, "Tabulator ID")
    assert found["batch"] == (2, "Batch #")
    assert found["count"] == (3, "#of Ballots")
    assert found["county"] == (0, "County")
    assert "location" not in found
    assert match_signature(["County", "Tabulator ID", "Location"], manifest) is None


# ---------------------------------------------------------------------------
# County manifests


@pytest.mark.parametrize(
    "header",
    [
        "County,Tabulator ID,Batch,# of Ballot Cards,Location",
        "County,Tabulator,Batch,# of Ballots,Location",
        "County,Device ID,Batch,# of Ballots,Location",
        "County,Device,Batch,#of Ballots,Location",
        "County,Scanner ID,Batch,# of Ballots,Location",
        "COUNTY,TABULATOR ID,BATCH ,# OF BALLOTS CARDS,LOCATION",
        "County,Tabulator,Batch ,# of Ballot,Box # Location",
        "County,Tabulator,Batch #,# in Batch,Location",
        "County,Tabulator,ICC Batch #,Number of Ballots Batched,Bin/Bag # Ballots Stored",
        "County,Tabulator ID,TrackingId,BatchCount,BallotStorageBox",
        "County,Tabulator,Batch,. of Ballots,Location",
        "County,Tabulator,Batch,# Cards,Location",
        "County,Tabulator ID,Batch,#Ballot Cards,Location,,,,",
        '"County","Tabulator ID","Batch",". of Ballots","Location"',
        "County,Tabulator ID,Batch,# of Ballot Cards,Location,Comments",
    ],
)
def test_manifest_header_variants(header: str) -> None:
    result = classify_bytes(csv_bytes(header, "Adams,101,1,100,Box 1", "Adams,101,2,98,Box 1"))
    assert result.kind == "county_manifest"
    assert result.county == "Adams"
    assert result.evidence["format"] == "csv"
    assert result.evidence["data_rows"] == 2


def test_manifest_county_named_in_header_cell_or_absent() -> None:
    named = classify_bytes(csv_bytes("Saguache,Tabulator ID,Batch,# of Ballot Cards,Location", ",1,1,25,B"))
    assert named.kind == "county_manifest"
    assert named.county == "Saguache"
    assert named.evidence["county_from_header_cell"] == "Saguache"
    blank = classify_bytes(csv_bytes(",Device ID,Batch,# of Ballots,Location", "Otero,1,1,25,B"))
    assert (blank.kind, blank.county) == ("county_manifest", "Otero")


def test_manifest_blank_continuation_rows_and_spelling_variants() -> None:
    data = csv_bytes(
        "County,Tabulator ID,Batch,# of Ballot Cards,Location",
        "BACA,201,1,25,BCC-1",
        ",201,2,25,BCC-1",
        "Baca ,201,3,25,BCC-1",
    )
    result = classify_bytes(data)
    assert (result.kind, result.county) == ("county_manifest", "Baca")
    assert "unrecognized_county_values" not in result.evidence


def test_manifest_with_a_typo_row_keeps_its_county_and_reports_the_typo() -> None:
    data = csv_bytes(
        "County,Tabulator ID,Batch,# of Ballot Cards,Location",
        "Prowers,102,1,25,Box 1",
        "Prowes,102,4,25,Box 1",
        "Prowers,102,5,25,Box 1",
    )
    result = classify_bytes(data)
    assert result.county == "Prowers"
    assert result.evidence["unrecognized_county_values"] == ["Prowes"]


def test_manifest_with_one_unrecognized_county_name() -> None:
    header = "County,Tabulator,Batch,# of Ballots,Location"
    assert classify_bytes(csv_bytes(header, "Atlantis,1,1,5,A")).county == "Atlantis"
    assert classify_bytes(csv_bytes(header, "Atl\x01antis,1,1,5,A")).county is None
    assert classify_bytes(csv_bytes(header, "X" * 65 + ",1,1,5,A")).county is None


def test_manifest_naming_two_counties_has_no_county() -> None:
    data = csv_bytes("County,Tabulator,Batch,# of Ballots,Location", "Adams,1,1,5,A", "Boulder,1,1,5,A")
    result = classify_bytes(data)
    assert result.kind == "county_manifest"
    assert result.county is None
    assert result.evidence["counties"] == ["Adams", "Boulder"]


def test_manifest_with_bom() -> None:
    data = codecs.BOM_UTF8 + csv_bytes("County,Tabulator,Batch,# of Ballots,Location", "Adams,101,1,100,B")
    result = classify_bytes(data)
    assert (result.kind, result.county, result.evidence["encoding"]) == (
        "county_manifest",
        "Adams",
        "utf-8-sig",
    )
    assert result.evidence["matched_columns"] == {
        "tabulator": "Tabulator",
        "batch": "Batch",
        "count": "# of Ballots",
        "county": "County",
        "location": "Location",
    }


def test_latin1_falls_back_and_still_classifies() -> None:
    data = csv_bytes(
        "County,Tabulator ID,Batch,# of Ballot Cards,Location",
        "Alamosa,1,1,5,Caja Peña",
        encoding="latin-1",
    )
    with pytest.raises(UnicodeDecodeError):
        data.decode("utf-8")
    result = classify_bytes(data)
    assert (result.kind, result.county, result.evidence["encoding"]) == (
        "county_manifest",
        "Alamosa",
        "latin-1",
    )


def test_utf16_with_bom() -> None:
    data = "County,Tabulator,Batch,# of Ballots,Location\r\nWeld,1,1,5,A\r\n".encode("utf-16")
    result = classify_bytes(data)
    assert (result.kind, result.county, result.evidence["encoding"]) == ("county_manifest", "Weld", "utf-16")


def test_excel_quote_wrapping() -> None:
    data = csv_bytes('="County",="Tabulator",="Batch",="# of Ballots"', '="Mesa",="1",="1",="5"')
    result = classify_bytes(data)
    assert (result.kind, result.county) == ("county_manifest", "Mesa")


@pytest.mark.parametrize("delimiter", ["\t", ";", "|"])
def test_other_delimiters(delimiter: str) -> None:
    rows = [["County", "Tabulator", "Batch", "# of Ballots"], ["Lake", "1", "1", "5"]]
    data = csv_bytes(*(delimiter.join(row) for row in rows))
    result = classify_bytes(data)
    assert (result.kind, result.county, result.evidence["delimiter"]) == (
        "county_manifest",
        "Lake",
        delimiter,
    )


# ---------------------------------------------------------------------------
# The audit center's CORLA exports


CORLA_EXPORTS = [
    (
        "contest_ids_by_county",
        ["county_id,county_name,contest_name,contest_id", "1,Adams,United States Senator,5278"],
        "Adams",
    ),
    (
        "contests_list",
        [
            "contest_name,audit_reason,random_audit_status,winners_allowed,ballot_card_count,"
            "contest_ballot_card_count,winners,min_margin,risk_limit,audited_sample_count",
            'Mayor,county_wide_contest,in_progress,1,100,90,"[""A""]",10,0.03,25',
        ],
        None,
    ),
    (
        "selection_list",
        ["min_margin,contest_name,contest_cvr_ids", '12,Mayor,"[1,2,3,3]"', "0,Coroner,[]"],
        None,
    ),
    (
        "cvr_comparison",
        [
            "county_name,contest_name,imprinted_id,ballot_type,choice_per_voting_computer,"
            "audit_board_selection,consensus,record_type,audit_board_comment,timestamp,cvr_id",
            'Adams,Mayor,101-1-1,BS-1,"""A""","""A""",YES,uploaded,"",2026-11-17 09:00:00,1',
            'Adams,Mayor,101-1-2,BS-1,"""B""","""B""",YES,uploaded,"",2026-11-17 09:01:00,2',
        ],
        "Adams",
    ),
    (
        "manifest_cvr_comparison",
        [
            "county_name,scanner_id,batch_id,count_per_manifest,count_per_cvr_file,difference",
            "Adams,101,1,98,98,0",
            "Boulder,1,1,50,50,0",
        ],
        None,
    ),
    (
        "upload_status",
        [
            "name,filename,type,approx_count,size,status,timestamp,computed_hash,submitted_hash",
            "Weld,cvr.csv,cvr,10,100,IMPORTED_AS_CVR,2026-11-14 10:00:00,AB,AB",
            "Weld,manifest.csv,manifest,10,100,IMPORTED_AS_BALLOT_MANIFEST,2026-11-14 10:01:00,CD,CD",
        ],
        "Weld",
    ),
    (
        "discrepancy_report",
        [
            "2026 General Election Discrepancies,,,,,,",
            "county_name,contest_name,imprinted_id,choice_per_voting_computer,audit_board_selection,"
            "audit_reason,Discrepancy Reason",
            'Broomfield,Governor,104-109-47,"""A""","""B""",N/A,Wrong Ballot',
        ],
        "Broomfield",
    ),
    (
        "target_contests",
        [
            "County,Contest,Vote For,Lowest Winner,Highest Loser,Contest Margin,Diluted Margin,"
            "Risk Limit,Estimated # of CVRs to audit,# of CVRs,Remarks",
            "Colorado,Presidential Electors,1,1374175,1084812,289363,0.08,0.03,89,2554611,All counties",
            "Adams,Adams County Commissioner,1,88099,74153,13946,0.04,0.03,184,176893,",
        ],
        "Adams",
    ),
    ("seed", ["seed", "53417960661093690826"], None),
    (
        "upload_status",
        [
            "name,filename,hash_status,approx_count,size,status,timestamp",
            "Adams,manifest.csv,VERIFIED,100,2000,IMPORTED_AS_BALLOT_MANIFEST,2018-11-14 10:00:00",
        ],
        "Adams",
    ),
    ("file_hashes", ["county_name,cvr_export_hash", "Adams,ABCDEF", "Boulder,012345"], None),
    ("file_hashes", ["county_name,ballot_manifest_hash", "Adams,ABCDEF"], "Adams"),
    (
        "canonical_contest_list",
        ["CountyName,ContestName,ContestChoices", 'Adams,Mayor,"A,B"', 'Adams,Coroner,"C"'],
        "Adams",
    ),
    ("vote_totals", ["contest_name,choice,votes", "Mayor,A,10"], None),
    ("vote_totals", ["county_name,contest_name,choice,votes", "Weld,Mayor,A,10", "Weld,Mayor,B,7"], "Weld"),
]


def test_footer_and_multi_county_rows_are_not_unrecognized_counties() -> None:
    lines = CORLA_EXPORTS[7][1] + ["Multi-county,Congress 3,1,5,4,1,0.1,0.03,70,10,", "End of worksheet", "* " + "x" * 70]
    result = classify_bytes(csv_bytes(*lines))
    assert (result.kind, result.county) == ("target_contests", "Adams")
    assert result.evidence["multi_county_rows"] == 1
    assert result.evidence["footer_rows"] == 2
    assert "unrecognized_county_values" not in result.evidence


@pytest.mark.parametrize(
    ("header", "column"),
    [("County,Tabulator ID,Batch,# Number of Ballot Cards,Location", "# Number of Ballot Cards"),
     ("County,tablulator id,Batch,# of Ballot Cards,Location", "tablulator id")],
)  # fmt: skip
def test_2024_primary_manifest_header_typos(header: str, column: str, tmp_path: Path) -> None:
    data = csv_bytes(header, "Huerfano,1,1,25,Box 1")
    result = classify_bytes(data)
    assert (result.kind, result.county) == ("county_manifest", "Huerfano")
    assert column in result.evidence["matched_columns"].values()  # type: ignore[union-attr]
    path = tmp_path / "m.csv"
    path.write_bytes(data)
    assert load_county_manifest(path)[0].ballot_count == 25


def test_corla_json_exports_are_tables() -> None:
    seed = classify_bytes(b'[{"seed":"95291361867577332045"}]')
    assert (seed.kind, seed.evidence["seed"], seed.evidence["format"]) == ("seed", "95291361867577332045", "json")
    contests = classify_bytes(
        b'[{"contest_name":"Mayor","audit_reason":"county_wide_contest","ballot_card_count":100,'
        b'"min_margin":10,"risk_limit":0.03,"audited_sample_count":25}]'
    )
    assert contests.kind == "contests_list"
    status = classify_bytes(
        b'[{"name":"Larimer","filename":"m.csv","type":"manifest","status":"IMPORTED",'
        b'"computed_hash":"AB","submitted_hash":"AB"}]'
    )
    assert (status.kind, status.county) == ("upload_status", "Larimer")
    other = classify_bytes(b'{"metadata": {"contest": "Mayor"}}')
    assert (other.kind, other.evidence["format"], other.evidence["json_type"]) == ("unknown", "json", "dict")
    assert classify_bytes(b'["a", "b"]').evidence["json_type"] == "list"
    # Text that merely starts like JSON is still tried as CSV.
    assert classify_bytes(csv_bytes("[note],x", "1,2")).evidence["format"] == "csv"


def test_county_audit_report() -> None:
    data = make_xlsx(
        {
            "Summary": [["La Plata County Audit Report"], [], ["Audit Random Seed", SEED]],
            "Round 1": [["x"]],
            "Affirmation": [["y"]],
        }
    )
    result = classify_bytes(data)
    assert (result.kind, result.county) == ("county_audit_report", "La Plata")
    assert result.evidence["audit_random_seed"] == SEED
    assert extract_seed(data) == SEED


@pytest.mark.parametrize(("kind", "lines", "county"), CORLA_EXPORTS, ids=[k for k, _, _ in CORLA_EXPORTS])
def test_corla_exports(kind: str, lines: list[str], county: str | None) -> None:
    result = classify_bytes(csv_bytes(*lines))
    assert (result.kind, result.county) == (kind, county)


def test_title_row_above_the_header() -> None:
    result = classify_bytes(csv_bytes(*CORLA_EXPORTS[6][1]))
    assert result.evidence["header_row"] == 2


def test_multi_county_files_list_their_counties() -> None:
    result = classify_bytes(csv_bytes(*CORLA_EXPORTS[4][1]))
    assert result.evidence["county_count"] == 2
    target = classify_bytes(csv_bytes(*CORLA_EXPORTS[7][1]))
    assert target.evidence["statewide_rows"] == 1


def test_seed_csv_needs_a_seed_value() -> None:
    assert (
        classify_bytes(csv_bytes("seed", "53417960661093690826")).evidence["seed"] == "53417960661093690826"
    )
    assert classify_bytes(csv_bytes("seed", "not a seed")).kind == "unknown"


def test_misnamed_file_is_classified_by_content(tmp_path: Path) -> None:
    path = tmp_path / "CVRtoAuditBoardInterpretativeComparison.csv"
    path.write_bytes(csv_bytes(*CORLA_EXPORTS[4][1]))
    assert classify_file(path).kind == "manifest_cvr_comparison"


# ---------------------------------------------------------------------------
# HTML and unknown files


def test_html_page() -> None:
    page = b"<!DOCTYPE html>\n<html><head><title>\n  Audit   Center\n</title></head><body></body></html>"
    result = classify_bytes(page)
    assert result == Classification(
        "html_page", None, {"format": "html", "encoding": "utf-8", "title": "Audit Center"}
    )
    challenge = classify_bytes(
        b"<html><head><title>Just a moment...</title></head><body><script>window._cf_chl_opt={}</script>"
    )
    assert challenge.evidence["cloudflare_challenge"] is True


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("data", "format_"),
    [
        (b"", "empty"),
        (b"%PDF-1.7\n...", "pdf"),
        (b"\x89PNG\r\n\x1a\n....", "png"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1....", "ole2 (legacy Office)"),
        (b"abc\x00def\x00", "binary"),
        (b"PK\x03\x04 truncated", "zip"),
        (_zip({"word/document.xml": b"<w/>"}), "docx"),
        (_zip({"mimetype": b"application/vnd.oasis.opendocument.spreadsheet"}), "opendocument"),
    ],
)
def test_unknown_formats(data: bytes, format_: str) -> None:
    result = classify_bytes(data)
    assert result.kind == "unknown"
    assert result.evidence["format"] == format_


def test_unknown_csv_reports_its_first_row() -> None:
    result = classify_bytes(csv_bytes("county_name,member,sign_in_time", "Adams,A,10"))
    assert result.kind == "unknown"
    assert result.evidence["first_row"] == ["county_name", "member", "sign_in_time"]


# ---------------------------------------------------------------------------
# Workbooks


def test_xlsx_reader_reads_sheets_and_cells() -> None:
    data = make_xlsx({"First": [["a", ("n", "1.5"), None, "d"], [], ["x"]], "Second": [["y"]]})
    reader = XlsxReader(data)
    assert reader.sheet_names == ["First", "Second"]
    assert list(reader.rows("First")) == [["a", "1.5", "", "d"], [], ["x"]]
    assert list(reader.rows("First", max_rows=1)) == [["a", "1.5", "", "d"]]
    with pytest.raises(XlsxError):
        list(reader.rows("Third"))


def test_xlsx_reader_inline_strings_and_absolute_targets() -> None:
    data = make_xlsx({"S": [["inline", "cells"]]}, inline_strings=True, absolute_targets=True)
    assert list(XlsxReader(data).rows("S")) == [["inline", "cells"]]


def test_xlsx_reader_refuses_doctype_and_non_workbooks() -> None:
    with pytest.raises(XlsxError, match="document type"):
        XlsxReader(make_xlsx({"S": [["a"]]}, doctype_in_workbook=True))
    with pytest.raises(XlsxError):
        XlsxReader(b"not a zip")
    with pytest.raises(XlsxError):
        XlsxReader(_zip({"other.xml": b"<x/>"}))


def test_extract_seed() -> None:
    assert extract_seed(state_audit_report()) == SEED
    assert extract_seed(state_audit_report(f'="{SEED}"')) == SEED
    # 20 digits can't survive as a floating-point number: not a seed.
    assert extract_seed(state_audit_report(("n", "4.9006417086137856E19"))) is None
    assert extract_seed(state_audit_report(("n", SEED))) == SEED
    assert extract_seed(state_audit_report(None)) is None
    assert extract_seed(make_xlsx({"Other": [["Audit Random Seed", SEED]]})) is None
    assert extract_seed(b"not a workbook") is None


def test_state_audit_report() -> None:
    result = classify_bytes(state_audit_report())
    assert result.kind == "state_audit_report"
    assert result.county is None
    assert result.evidence["audit_random_seed"] == SEED
    assert result.evidence["sheets"] == ["Summary", "Adams County"]
    assert result.evidence["matched_label"] == "Audit Random Seed"
    # Titled as one, but with no seed row (yet).
    titled_without_seed = make_xlsx({"Summary": [["State Audit Report"], ["Audit Risk Limit", "0.03"]]})
    found = classify_bytes(titled_without_seed)
    assert (found.kind, found.evidence["audit_random_seed"]) == ("state_audit_report", None)


def test_xlsx_tables() -> None:
    target = make_xlsx(
        {
            "Sheet1": [
                ["2026 General Election Target Contest Selections"],
                [
                    "County",
                    "Contest",
                    "Vote For",
                    "Diluted Margin",
                    "Risk Limit",
                    "Estimated # of CVRs to audit",
                ],
                ["Colorado", "United States Senator", ("n", "1"), ("n", "0.06"), ("n", "0.03"), ("n", "118")],
                ["Adams", "Attorney General", ("n", "1"), ("n", "0.04"), ("n", "0.03"), ("n", "162")],
                ["Boulder", "Mayor", ("n", "1"), ("n", "0.05"), ("n", "0.03"), ("n", "149")],
            ]
        }
    )
    result = classify_bytes(target)
    assert (result.kind, result.county) == ("target_contests", None)
    assert result.evidence["sheet"] == "Sheet1"
    assert result.evidence["header_row"] == 2

    discrepancies = make_xlsx(
        {
            "Discrepancy Report": [
                ["2026 General Election Discrepancy Report"],
                ["county_name", "contest_name", "imprinted_id", "choice_per_voting_computer",
                 "audit_board_selection", "audit_reason", "Discrepancy Reason"],
                ["Adams", "Mayor", "101-1-1", '"A"', '"B"', "N/A", "Audit Board Error"],
                ["End of worksheet"],
            ]
        }
    )  # fmt: skip
    found = classify_bytes(discrepancies)
    assert (found.kind, found.county) == ("discrepancy_report", "Adams")
    assert found.evidence["footer_rows"] == 1


def test_per_contest_audit_workbook_has_no_single_county() -> None:
    header = ["county", "imprinted id", "scanner id", "batch id", "record id", "round", "audit board",
              "record type", "discrepancy", "consensus"]  # fmt: skip
    data = make_xlsx(
        {
            "Contests to Sheets": [["Contest Name", "Sheet Name"], ["Mayor", "Mayor"]],
            "Mayor": [
                ["Mayor"],
                header,
                ["Adams", "101-1-1", "101", "1", "1", "1", "1", "AUDITOR_ENTERED", "", "Yes"],
            ],
        }
    )
    result = classify_bytes(data)
    assert result.kind == "audit_activity_report"
    assert result.county is None
    assert result.evidence["matched_sheet_county"] == "Adams"
    assert result.evidence["sheet"] == "Mayor"


def test_unrecognized_workbook_lists_its_sheets() -> None:
    result = classify_bytes(make_xlsx({"Notes": [["hello"]], "More": [["world"]]}))
    assert result.kind == "unknown"
    assert result.evidence["sheets"] == ["Notes", "More"]
    assert result.evidence["sheet_count"] == 2


# ---------------------------------------------------------------------------
# Command line


def test_cli_classify_and_seed(tmp_path: Path) -> None:
    manifest = tmp_path / "anything.txt"
    manifest.write_bytes(csv_bytes("County,Tabulator,Batch,# of Ballots", "Adams,1,1,5"))
    report = tmp_path / "report.xlsx"
    report.write_bytes(state_audit_report())
    runner = CliRunner()
    result = runner.invoke(app, ["classify", str(manifest), str(report)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split("\t")[:3] == [str(manifest), "county_manifest", "Adams"]
    assert lines[1].split("\t")[:3] == [str(report), "state_audit_report", "-"]
    seed = runner.invoke(app, ["seed", str(report)])
    assert (seed.exit_code, seed.output) == (0, SEED + "\n")
    missing = runner.invoke(app, ["seed", str(manifest)])
    assert missing.exit_code == 1


@pytest.mark.parametrize(
    ("name", "county"),
    [
        ("Adams.csv", "Adams"),
        ("files/ClearCreek.csv", "Clear Creek"),
        ("round_1/county_manifest_Baca.csv", "Baca"),
        ("manifest-RioBlanco.csv", "Rio Blanco"),
        ("Manifest-Gunnison.csv", "Gunnison"),
        ("AdamsBallotManifest.csv", "Adams"),
        ("MontroseManifest.csv", "Montrose"),
        ("LaPlata_BallotManifest.csv", "La Plata"),
        ("Weld_CVR_Export_20260709140755.csv", "Weld"),
        ("county_report_Clear_Creek.xlsx", "Clear Creek"),
        ("Elbert.csv", "Elbert"),
        ("uploadStatus.csv", None),
        ("contest.csv", None),
        ("Lakewood.csv", None),
    ],
)
def test_county_from_filename(name: str, county: str | None) -> None:
    assert county_from_filename(name) == county
