"""Observation reports from marked spreadsheets and records, compared with each other and with CORLA's discrepancy report."""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.obsreport import MarksError, build_report, report_from_marks, report_from_record, sign_report
from corla_observer.receipts import ReceiptSigner
from corla_observer.reportcheck import CorlaDiscrepancy, compare_reports, compare_with_corla, read_corla_discrepancies

#: CORLA's published 2026 primary discrepancy report, in a clone of the audit center mirror ($CORLA_OBSERVER_MIRROR).
MIRROR_2026 = Path(os.environ.get("CORLA_OBSERVER_MIRROR") or "/srv/voting/audit/corla/scrapy/mirror") / "2026/primary/files/2026DiscrepancyReportPE.xlsx"


@pytest.fixture(scope="module")
def sheet(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, dict[str, Any], str]:
    """Testland's extract spreadsheet for board 1 (unmarked), the extract and its SHA-256."""
    from corla_observer.events import EventLog
    from corla_observer.extract import to_csv
    from corla_observer.quicksetup import populate
    from corla_observer.workspace import Workspace

    store = Workspace.open(tmp_path_factory.mktemp("ws") / "ws")
    with EventLog.open(store.root) as log:
        made = populate(store, log, "Testland", "coordinator:ann")
        built = [e for e in log.iter_events() if e.kind == "extract.built" and e.county == made.county]
    sha = built[-1].file_sha256
    extract = json.loads(store.get_bytes(sha))
    return to_csv(extract, sha, board=1), extract, sha


def _mark(text: str, marks: dict[int, tuple[str, str]], delimiter: str = ",") -> bytes:
    """The spreadsheet with ``marks`` (ballot row number from 0, to (match, heard instead)) filled in."""
    rows = list(csv.reader(io.StringIO(text)))
    at = next(i for i, r in enumerate(rows) if r[:1] == ["board"])
    m, h = rows[at].index("match"), rows[at].index("heard instead")
    for n, (match, heard) in marks.items():
        rows[at + 1 + n][m], rows[at + 1 + n][h] = match, heard
    out = io.StringIO()
    csv.writer(out, delimiter=delimiter).writerows(rows)
    return out.getvalue().encode()


def test_a_marked_spreadsheet_gives_the_report_of_the_same_entries_online(sheet: tuple[str, dict[str, Any], str]) -> None:
    text, extract, sha = sheet
    ballots = [b for b in extract["ballots"] if int(b["board"]) == 1]
    data = _mark(text, {0: ("y", ""), 1: ("", "Bob for Governor"), 2: ("Other board", ""), 3: ("no", "")}, delimiter=";")
    report = report_from_marks(data, observer="observer:ann")
    assert report["extract_sha256"] == sha and report["board"] == "1" and report["county"] == "Testland"
    assert [b["verdict"] for b in report["ballots"]] == ["match", "discrepancy", "other_board", "discrepancy"]
    assert report["ballots"][1]["note"] == "Bob for Governor"
    # The same observations entered online give the same report.
    online = [
        {"type": "entry", "position": b["position"], "imprinted_id": b["imprinted_id"], "extract_sha256": sha, "content": content}
        for b, content in zip(ballots, ({"match": True, "note": ""}, {"match": False, "note": "Bob for Governor"}), strict=False)
    ] + [{"type": "other_board", "position": ballots[2]["position"], "imprinted_id": ballots[2]["imprinted_id"], "extract_sha256": sha, "content": {}},
         {"type": "entry", "position": ballots[3]["position"], "imprinted_id": ballots[3]["imprinted_id"], "extract_sha256": sha, "content": {"match": False, "note": ""}}]  # fmt: skip
    assert build_report(online, observer="observer:ann", county="Testland", board="1", extract_sha256=sha) == report
    assert compare_reports(report, report).differences == []
    changed = report_from_marks(_mark(text, {0: ("n", "Cy"), 1: ("", "Bob for Governor"), 2: ("other", ""), 3: ("no", "")}), observer="b")
    result = compare_reports(report, changed)
    assert [(f["kind"], f.get("a"), f.get("b")) for f in result.differences] == [("verdicts_differ", "match", "discrepancy")]


def test_marks_that_cannot_be_read_are_errors_naming_the_row(sheet: tuple[str, dict[str, Any], str]) -> None:
    text, _, _ = sheet
    with pytest.raises(MarksError, match=r"row \d+ .*'maybe' is not a mark"):
        report_from_marks(_mark(text, {0: ("maybe", "")}), observer="o")
    with pytest.raises(MarksError, match="marked as a match, with 'Bob' heard instead"):
        report_from_marks(_mark(text, {1: ("yes", "Bob")}), observer="o")
    with pytest.raises(MarksError, match="no extract SHA-256"):
        report_from_marks(text.replace("Extract,", "Something,").encode(), observer="o")
    with pytest.raises(MarksError, match="not an extract spreadsheet"):
        report_from_marks(b"a,b\n1,2\n", observer="o")
    with pytest.raises(MarksError, match="no ballots for board 7"):
        report_from_marks(text.encode(), observer="o", board="7")


def test_a_downloaded_record_gives_its_acknowledged_entries_report() -> None:
    sha = "e" * 64
    entry = {"type": "entry", "position": 1, "imprinted_id": "1-1-1", "extract_sha256": sha, "device_seq": 2, "content": {"match": False, "note": "x"}}
    record = {"format": "corla-observer-record", "observer": "observer:ann", "county": "Delta", "board": "1", "extract_sha256": sha,
              "entries": [{"payload": entry, "receipt": {"receipt": {}}},
                          {"payload": entry | {"device_seq": 3, "content": {"match": True, "note": ""}}, "receipt": None}]}  # fmt: skip
    report = report_from_record(record)
    assert report["summary"]["discrepancies"] == 1 and report["ballots"][0]["note"] == "x"  # the unacknowledged one is left out


def _corla_csv(rows: list[tuple[str, str, str, str]]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["2026 General Election Discrepancies"])
    w.writerow(["county_name", "contest_name", "imprinted_id", "choice_per_voting_computer", "audit_board_selection", "consensus", "audit_board_comment", "audit_reason", "Discrepancy Reason"])
    for county, contest, imprinted, reason in rows:
        w.writerow([county, contest, imprinted, '"Ann"', '"Bob"', "YES", "", "", reason])
    return out.getvalue().encode()


def test_compared_with_corlas_discrepancy_report() -> None:
    sha = "e" * 64

    def c(position: int, row: int, contest: str, outcome: str) -> list[dict[str, Any]]:
        return [{"type": "contest", "position": position, "imprinted_id": f"1-1-{position}", "extract_sha256": sha,
                 "content": {"row": row, "rows": 2, "reading": r, "outcome": outcome, "heard": "Bob", "contest": contest, "expected": "Ann"}} for r in (1, 2)]  # fmt: skip

    def tap(position: int, match: bool) -> dict[str, Any]:
        return {"type": "entry", "position": position, "imprinted_id": f"1-1-{position}", "extract_sha256": sha, "content": {"match": match, "note": ""}}

    payloads = [tap(1, True), tap(2, False), tap(3, True), *c(4, 1, "Governor", "heard_instead"), *c(4, 2, "Mayor", "confirmed"),
                *c(5, 1, "Governor", "heard_instead"), *c(5, 2, "Mayor", "confirmed"), tap(6, False)]  # fmt: skip
    report = build_report(payloads, observer="o", county="Delta", board="1", extract_sha256=sha)
    corla = read_corla_discrepancies(_corla_csv([
        ("Delta", "Governor", "1-1-3", "Audit Board Error"),  # observer heard a match
        ("Delta", "Governor", "1-1-4", "Voter Intent"),  # agrees
        ("Delta", "Mayor", "1-1-5", "Voter Intent"),  # another contest
        ("Delta", "Governor", "1-1-6", "Wrong Ballot"),  # agrees (no contests named)
        ("Delta", "Governor", "1-1-9", "Wrong Ballot"),  # listed, not observed
        ("Weld", "Governor", "1-1-2", "Wrong Ballot"),  # another county's ballot
    ]))  # fmt: skip
    assert corla[0] == CorlaDiscrepancy("Delta", "1-1-3", "Governor", "Ann", "Bob", "Audit Board Error")
    result = compare_with_corla(report, corla, listed=[f"1-1-{n}" for n in range(1, 10)])
    kinds = {f["imprinted_id"]: f["kind"] for f in result.findings}
    assert kinds == {"1-1-1": "agree", "1-1-2": "observer_only", "1-1-3": "corla_only", "1-1-4": "agree",
                     "1-1-5": "contests_differ", "1-1-6": "agree", "1-1-9": "not_observed"}  # fmt: skip
    assert len(result.differences) == 4


@pytest.mark.skipif(not MIRROR_2026.exists(), reason="no audit center mirror")
def test_reads_corlas_published_discrepancy_workbook() -> None:
    found = read_corla_discrepancies(MIRROR_2026.read_bytes())
    assert len(found) == 16 and found[0] == CorlaDiscrepancy("Arapahoe", "104-94-93", "Secretary of State - REP", "", "James Wiley", "Audit Board Error")


def test_the_report_commands(sheet: tuple[str, dict[str, Any], str], tmp_path: Path) -> None:
    text, extract, sha = sheet
    first = next(b for b in extract["ballots"] if int(b["board"]) == 1)
    marked = tmp_path / "marked.csv"
    marked.write_bytes(_mark(text, {0: ("", "Bob for Governor")}))
    runner = CliRunner()
    shown = runner.invoke(app, ["report", "show", str(marked), "--observer", "observer:ann"])
    assert shown.exit_code == 0, shown.output
    assert "1 ballots: 0 matched, 1 with discrepancies" in shown.output and "Bob for Governor" in shown.output
    # The server's signed report of the same entry, checked with its key, agrees with the spreadsheet.
    signer = ReceiptSigner.generate()
    online = build_report([{"type": "entry", "position": first["position"], "imprinted_id": first["imprinted_id"], "extract_sha256": sha,
                            "content": {"match": False, "note": "Bob for Governor"}}], observer="observer:ann", county="Testland", board="1", extract_sha256=sha)  # fmt: skip
    signed = tmp_path / "signed.json"
    signed.write_text(json.dumps(sign_report(signer, online, device_id="d-" + "a" * 32, head=(3, "0" * 64))))
    agreed = runner.invoke(app, ["report", "compare", str(marked), str(signed), "--server-key", signer.public_b64])
    assert agreed.exit_code == 0 and "signature checked" in agreed.output, agreed.output
    forged = json.loads(signed.read_text())
    forged["report"]["summary"]["discrepancies"] = 0
    signed.write_text(json.dumps(forged))
    assert runner.invoke(app, ["report", "compare", str(marked), str(signed), "--server-key", signer.public_b64]).exit_code == 2
    corla_file = tmp_path / "discrepancies.csv"
    corla_file.write_bytes(_corla_csv([("Testland", "Governor", first["imprinted_id"], "Audit Board Error")]))
    against = runner.invoke(app, ["report", "corla", str(marked), str(corla_file)])
    assert against.exit_code == 0 and "agree: 1" in against.output, against.output
    corla_file.write_bytes(_corla_csv([]))
    assert runner.invoke(app, ["report", "corla", str(marked), str(corla_file)]).exit_code == 1


def test_reports_from_a_published_log(tmp_path: Path) -> None:
    """``report log``: each device's report from the JSONL log, the same as the server signs for it."""
    from corla_observer.events import EventLog
    from corla_observer.obsreport import device_reports

    store = tmp_path / "ws"
    sha = "e" * 64
    with EventLog.open(store) as log:
        for seq, (device, match) in enumerate((("d-" + "a" * 32, True), ("d-" + "b" * 32, False)), start=1):
            payload = {"type": "entry", "position": 1, "imprinted_id": "1-1-1", "extract_sha256": sha, "device_id": device, "device_seq": seq,
                       "content": {"match": match, "note": "" if match else "heard Bob"}}  # fmt: skip
            log.append("observation.entry", "observer:ann", county="Delta", board="1", device_id=device, data={"payload": payload})
        found = device_reports(list(log.iter_events()))
    assert [(d[-1], r["summary"]["matches"]) for d, r in found] == [("a", 1), ("b", 0)]
    jsonl = next(store.rglob("events.jsonl"))
    result = CliRunner().invoke(app, ["report", "log", str(jsonl), "--county", "Delta", "--out", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    assert "2 reports" in result.output and "heard Bob" in result.output
    written = sorted((tmp_path / "out").glob("*.json"))
    assert len(written) == 2
    compared = CliRunner().invoke(app, ["report", "compare", str(written[0]), str(written[1])])
    assert compared.exit_code == 1 and "verdicts_differ" in compared.output, compared.output
