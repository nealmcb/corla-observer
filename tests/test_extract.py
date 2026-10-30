"""Fast tests for the observers' extract (corla_observer.extract): building, hashing, pages, print, text and CSV."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.datamap import build_map
from corla_observer.events import EventLog, canonical_json
from corla_observer.extract import (
    CHECK_STEPS,
    KEEP_OUT_OF_SIGHT,
    LAYOUTS,
    PAGE_LINES,
    PREDICTED_NOTE,
    ExtractError,
    block_lines,
    board_pages,
    build_extract,
    extract_sha256,
    grouped,
    latest_extract,
    page_index,
    page_line,
    paginate,
    print_contest,
    print_names,
    print_lines,
    print_rows,
    print_vote,
    provenance,
    qr_extract_payload,
    qr_payload,
    render_provenance_text,
    render_text,
    screen_line,
    spreadsheet_cell,
    to_csv,
)
from corla_observer.predict import predict, record_prediction
from corla_observer.seeds import enter_seed
from corla_observer.uploads import receive
from corla_observer.workspace import Workspace
from synthetic import CONTEST_IDS, contests_list, election_files
from test_datamap import fetch

SEED = "12345678901234567890"
ACTOR = "coordinator:alice"


def adams_cvr(ids: list[str], *, omit: str, redact: str, overvote: str) -> bytes:
    """A CVR with every listed Adams ballot but ``omit``, ``redact`` redacted and ``overvote`` overvoted."""
    lines = [
        '"2026 Synthetic General","5.17.17.1",,,,,,,,',
        ',,,,,,"Governor - DEM (Vote For=1)","Governor - DEM (Vote For=1)","County Clerk (Vote For=1)","County Clerk (Vote For=1)"',
        ",,,,,,Ann,Bob,=Cy,Di",
        "CvrNumber,TabulatorNum,BatchId,RecordId,ImprintedId,BallotType,,,,",
    ]
    for n, imprinted in enumerate(ids, start=1):
        if imprinted == omit:
            continue
        tab, batch, record = imprinted.split("-")
        votes = "*,*,*,*" if imprinted == redact else "1,1,0,1" if imprinted == overvote else ("1,0,0,1" if n % 2 else "0,0,1,0")
        lines.append(f'{n},{tab},{batch},{record},="{imprinted}",BT1,{votes}')
    return ("\r\n".join(lines) + "\r\n").encode()


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """The synthetic election with a seed, a prediction and Adams's redacted CVR."""
    ws = Workspace.open(tmp_path / "ws")
    with EventLog.open(ws.root) as log:
        fetch(ws, log, election_files(finalReports_ContestsListRound1_csv=contests_list(), finalReports_ContestIDsByCounty_csv=CONTEST_IDS))
        enter_seed(log, SEED, ACTOR, "announced at the dice roll")
        record_prediction(ws, log, predict(build_map(log, ws), counties=["Adams"]), ACTOR)
        ids = [f"101-1-{n}" for n in range(1, 31)] + [f"101-2-{n}" for n in range(1, 21)]
        drawn = adams_draws(ws, log)
        cvr = ws.put_bytes(adams_cvr(ids, omit=drawn[0], redact=drawn[1], overvote=drawn[2]))
        log.append(
            "file.received",
            ACTOR,
            county="Adams",
            file_sha256=cvr.sha256,
            data={"kind": "redacted_cvr", "original_name": "adams.csv", "sha256": cvr.sha256, "size": cvr.size},
        )
    return ws


def adams_draws(ws: Workspace, log: EventLog) -> list[str]:
    """Adams's distinct drawn ballots, in pull order."""
    from corla_observer.extract import pull_order_key
    from corla_observer.predict import latest_prediction

    latest = latest_prediction(ws, log)
    assert latest is not None
    prediction: Any = latest[1]
    ids = {str(d["imprinted_id"]) for c in prediction["contests"] for d in c["draws"] if d["county"] == "Adams"}
    return sorted(ids, key=pull_order_key)


def test_predicted_extract_is_deterministic_and_complete(ws: Workspace) -> None:
    with EventLog.open(ws.root) as log:
        drawn = adams_draws(ws, log)
        built = build_extract(ws, log, "adams", boards=2, actor=ACTOR)
        again = build_extract(ws, log, "Adams", boards=2, actor=ACTOR)
        events = [e for e in log.iter_events() if e.kind == "extract.built"]
    extract = built.extract
    assert again.sha256 == built.sha256 == extract_sha256(extract)
    assert hashlib.sha256(ws.get_bytes(built.sha256)).hexdigest() == built.sha256
    assert ws.get_bytes(built.sha256) == canonical_json(extract)
    assert extract["order"] == "predicted" and "PREDICTED" in extract["order_note"]
    assert [b["imprinted_id"] for b in extract["ballots"]] == drawn
    assert [b["position"] for b in extract["ballots"]] == list(range(1, len(drawn) + 1))
    assert {b["board"] for b in extract["ballots"]} == {1, 2}
    assert extract["seed"] == SEED and extract["inputs"]["seed"] == SEED
    first, second, third = extract["ballots"][:3]
    assert first["status"] == "not_in_cvr" and first["votes"] == []
    assert second["status"] == "redacted" and second["votes"] == []
    assert third["status"] == "votes" and ["Governor - DEM", "overvote: Ann + Bob"] in third["votes"]
    assert all(b["location"] == "Box 1" for b in extract["ballots"])
    # Statewide picks alongside the county's own.
    scopes = {scope for b in extract["ballots"] for _, scope in b["picks"]}
    assert scopes == {"statewide", "county"}
    assert extract["contests"]["Governor - DEM"]["scope"] == "statewide"
    # The event records every input's hash, so anyone can recompute the extract.
    event: Any = events[-1]
    assert event.file_sha256 == built.sha256 and event.county == "Adams"
    inputs = event.data["inputs"]
    assert isinstance(inputs, dict) and inputs["pull_list"] is None and inputs["redacted_cvr"] and inputs["prediction"]
    assert inputs["county_manifest"] and inputs["manifests"] and inputs["seed"] == SEED
    assert event.data["order"] == "predicted" and event.data["purpose"] == "audit"
    assert hashlib.sha256(ws.get_bytes(str(event.data["page_index_sha256"]))).hexdigest() == event.data["page_index_sha256"]
    assert json.loads(ws.get_bytes(str(event.data["page_index_sha256"]))) == page_index(extract, built.sha256)
    # Each board's printout in each layout, stored, its hash recorded: the same bytes as drawn anew.
    from corla_observer.pdfextract import render_pdf

    pdfs = event.data["pdfs"]
    assert isinstance(pdfs, dict) and set(pdfs) == set(LAYOUTS) and built.pdfs == pdfs
    assert all(set(by_board) == {"1", "2"} for by_board in pdfs.values())
    assert ws.get_bytes(pdfs["landscape-3"]["2"]) == render_pdf(extract, built.sha256, 2, "landscape-3", software=event.data["software"])
    assert event.data["pages"]["portrait-1"] == {"1": len(board_pages(extract, built.sha256, 1)), "2": len(board_pages(extract, built.sha256, 2))}


def test_extract_needs_a_cvr_and_a_list_or_a_prediction(tmp_path: Path) -> None:
    ws = Workspace.open(tmp_path / "ws")
    with EventLog.open(ws.root) as log:
        fetch(ws, log, election_files(finalReports_ContestsListRound1_csv=contests_list(), finalReports_ContestIDsByCounty_csv=CONTEST_IDS))
        record_prediction(ws, log, predict(build_map(log, ws), counties=["Adams"], seed=SEED), ACTOR)
        with pytest.raises(ExtractError, match="no redacted CVR"):
            build_extract(ws, log, "Adams")
        cvr = ws.put_bytes(adams_cvr([f"101-1-{n}" for n in range(1, 31)], omit="", redact="", overvote=""))
        log.append("file.received", ACTOR, county="Adams", file_sha256=cvr.sha256,
                   data={"kind": "redacted_cvr", "original_name": "adams.csv", "sha256": cvr.sha256, "size": cvr.size})  # fmt: skip
        with pytest.raises(ExtractError, match="no seed"):
            build_extract(ws, log, "Adams")
        enter_seed(log, "9" * 20, ACTOR, "test")
        with pytest.raises(ExtractError, match="no prediction from the seed in effect"):
            build_extract(ws, log, "Adams")
        enter_seed(log, SEED, ACTOR, "test")
        assert build_extract(ws, log, "Adams").extract["order"] == "predicted"
        with pytest.raises(ExtractError, match="not a Colorado county"):
            build_extract(ws, log, "Atlantis")


def test_extract_from_a_pull_list_needs_no_prediction_or_seed(tmp_path: Path) -> None:
    """A pull list and a redacted CVR are enough: no seed, no prediction, no audit center files."""
    ws = Workspace.open(tmp_path / "ws")
    ids = [f"101-1-{n}" for n in range(1, 11)]
    with EventLog.open(ws.root) as log:
        cvr = ws.put_bytes(adams_cvr(ids, omit="", redact="", overvote=""))
        log.append("file.received", ACTOR, county="Adams", file_sha256=cvr.sha256,
                   data={"kind": "redacted_cvr", "original_name": "adams.csv", "sha256": cvr.sha256, "size": cvr.size})  # fmt: skip
        rows = ["County,Imprinted ID,Audit Board"] + [f"Adams,{i},Audit Board 1" for i in ids[:4]]
        assert receive(ws, log, "Adams", "pull_list", io.BytesIO(("\r\n".join(rows) + "\r\n").encode()), "pull.csv", ACTOR).accepted
        built = build_extract(ws, log, "Adams", actor=ACTOR)
    extract = built.extract
    assert extract["order"] == "pull_list" and extract["seed"] is None
    assert extract["inputs"]["prediction"] is None and extract["inputs"]["manifests"] is None
    assert "not_predicted" not in extract["checks"]
    assert [b["imprinted_id"] for b in extract["ballots"]] == ids[:4]
    assert all(b["picks"] == [] and b["certain"] for b in extract["ballots"])


def test_a_test_county_is_self_contained(tmp_path: Path) -> None:
    """Testland alone in a workspace: predicted from its own seed, manifest and fixed sample count, or from its pull list."""
    from corla_observer import testcounty

    ws = Workspace.open(tmp_path / "ws")
    files = testcounty.make().files()
    with EventLog.open(ws.root) as log:
        for name, kind in ((testcounty.MANIFEST_NAME, "manifest"), (testcounty.CVR_NAME, "redacted_cvr")):
            assert receive(ws, log, "Testland", kind, io.BytesIO(files[name]), name, ACTOR).accepted
        predicted = build_extract(ws, log, "Testland", actor=ACTOR)
        assert predicted.extract["order"] == "predicted" and predicted.extract["purpose"] == "practice"
        assert predicted.extract["seed"] == testcounty.SEED
        assert len({b["position"] for b in predicted.extract["ballots"]}) <= testcounty.SAMPLE_COUNT
        assert predicted.extract["inputs"]["manifests"] is None and predicted.extract["inputs"]["overrides"] is None
        stored = json.loads(ws.get_bytes(str(predicted.extract["inputs"]["prediction"])))
        assert stored["contests"][0]["sample_count"] == testcounty.SAMPLE_COUNT and not stored["contests"][0]["estimated"]
        assert not any(e.kind == "prediction.made" for e in log.iter_events())
        assert all(b["location"] for b in predicted.extract["ballots"])
        smaller = build_extract(ws, log, "Testland", actor=ACTOR, sample_count=10)
        assert len({b["position"] for b in smaller.extract["ballots"]}) <= 10
        enter_seed(log, SEED, ACTOR, "test")
        assert build_extract(ws, log, "Testland", actor=ACTOR).extract["seed"] == SEED
        name = testcounty.PULL_LIST_NAME
        assert receive(ws, log, "Testland", "pull_list", io.BytesIO(files[name]), name, ACTOR).accepted
        listed = build_extract(ws, log, "Testland", actor=ACTOR)
    assert listed.extract["order"] == "pull_list" and listed.extract["inputs"]["prediction"] is None
    assert len(listed.extract["ballots"]) == testcounty.PULL_LIST_SIZE


def test_extract_from_the_pull_list(ws: Workspace, tmp_path: Path) -> None:
    with EventLog.open(ws.root) as log:
        drawn = adams_draws(ws, log)
        extra = next(f"101-2-{n}" for n in range(20, 0, -1) if f"101-2-{n}" not in drawn)
        listed = list(reversed(drawn[1:])) + [extra]  # the first predicted ballot left out, one not predicted added
        rows = ["County,Imprinted ID,Audit Board,Storage Location"]
        rows += [f"Adams,{i},Audit Board {1 + n % 2},Bin {n}" for n, i in enumerate(listed)]
        result = receive(ws, log, "Adams", "pull_list", io.BytesIO(("\r\n".join(rows) + "\r\n").encode()), "pull.csv", ACTOR)
        assert result.accepted
        built = build_extract(ws, log, "Adams", actor=ACTOR)
    extract = built.extract
    assert extract["order"] == "pull_list" and extract["inputs"]["pull_list"] == result.sha256
    assert [b["imprinted_id"] for b in extract["ballots"]] == listed
    assert [b["board"] for b in extract["ballots"][:4]] == [1, 2, 1, 2]
    assert [b["board_position"] for b in extract["ballots"][:4]] == [1, 1, 2, 2]
    assert extract["ballots"][0]["location"] == "Bin 0"  # the list's own location column
    assert extract["checks"]["predicted_not_listed"] == [drawn[0]]
    assert extract["checks"]["not_predicted"] == [extra]
    assert extract["ballots"][-1]["picks"] == [] and not extract["ballots"][-1]["predicted"]
    assert extract["ballots"][-1]["status"] == "votes"  # in the CVR, though not predicted


def test_pages_and_page_hashes(ws: Workspace) -> None:
    with EventLog.open(ws.root) as log:
        built = build_extract(ws, log, "Adams", boards=1, actor=ACTOR)
    extract, sha = built.extract, built.sha256
    ballots = extract["ballots"]
    pages = board_pages(extract, sha, 1, "portrait-1")
    # The pagination rule: whole ballots, as many as fit in PAGE_LINES lines.
    assert [b for p in pages for b in p.ballots] == ballots
    for page in pages:
        assert sum(block_lines(b) for b in page.ballots) <= PAGE_LINES or len(page.ballots) == 1
    assert [len(p) for p in paginate(ballots)] == [len(p.ballots) for p in pages]
    # A page's hash, as docs/EXTRACT.md defines it.
    page = pages[0]
    record = {
        "format": 6,
        "kind": "observer_extract_page",
        "layout": "portrait-1",
        "extract_sha256": sha,
        "county": "Adams",
        "board": 1,
        "page": 1,
        "pages": len(pages),
        "ballots": list(page.ballots),
    }
    text = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert page.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    # Several columns: each filled in turn; the layout is part of the page's record, so its hash differs.
    assert set(LAYOUTS) == {"portrait-1", "portrait-2", "landscape-2", "landscape-3"}
    for name in ("portrait-2", "landscape-2", "landscape-3"):
        columns, lines = LAYOUTS[name]
        split = board_pages(extract, sha, 1, name)
        assert [b for p in split for b in p.ballots] == ballots
        for p in split:
            assert 1 <= len(p.columns) <= columns and [b for c in p.columns for b in c] == list(p.ballots)
            assert all(sum(block_lines(b) for b in c) <= lines or len(c) == 1 for c in p.columns)
        other = dict(record, layout=name, pages=len(split), ballots=list(split[0].ballots))
        assert split[0].sha256 == hashlib.sha256(canonical_json(other)).hexdigest() != page.sha256
    two_up = board_pages(extract, sha, 1, "landscape-2")
    index = page_index(extract, sha)
    assert index["format"] == 6 and set(index["layouts"]) == set(LAYOUTS)
    assert [e["sha256"] for e in index["layouts"]["landscape-2"]["boards"]["1"]] == [p.sha256 for p in two_up]
    assert index["layouts"]["landscape-3"]["columns"] == 3 and index["layouts"]["portrait-1"]["column_lines"] == PAGE_LINES
    # The QR code carries the page's hash alone (it commits to the extract's), in alphanumeric mode, error correction M.
    import segno

    assert qr_payload(page) == f"COX2:{page.sha256.upper()}" and len(qr_payload(page)) == 69
    code = segno.make(qr_payload(page), error="m", micro=False)
    assert code.mode == "alphanumeric" and code.designator == "4-M"
    assert qr_extract_payload(sha) == f"COX2E:{sha.upper()}"
    assert segno.make(qr_extract_payload(sha), error="m", micro=False).designator == "4-M"
    # Every page hash differs, and changes if any ballot on the page changes.
    assert len({p.sha256 for p in pages}) == len(pages)
    changed = json.loads(json.dumps(extract))
    changed["ballots"][0]["imprinted_id"] = "101-9-9"
    assert board_pages(changed, sha, 1)[0].sha256 != page.sha256


def test_paginate_rule() -> None:
    def ballot(votes: int) -> dict[str, object]:
        return {"votes": [["c", "v"]] * votes}

    assert block_lines(ballot(0)) == 2 and block_lines(ballot(3)) == 4
    sizes = [len(p) for p in paginate([ballot(10)] * 5, page_lines=30)]
    assert sizes == [2, 2, 1]  # 11 lines each
    assert PAGE_LINES == 76
    assert [len(p) for p in paginate([ballot(9)] * 7)] == [7]  # 70 lines, within PAGE_LINES
    assert [len(p) for p in paginate([ballot(9)] * 8)] == [7, 1]
    assert [len(p) for p in paginate([ballot(50), ballot(1)], page_lines=30)] == [1, 1]
    # Two columns: the left column fills, then the right, then a new page.
    from corla_observer.extract import column_split

    split = column_split([ballot(10)] * 7, 30, 2)
    assert [[len(c) for c in page] for page in split] == [[2, 2], [2, 1]]
    assert [len(p) for p in paginate([ballot(10)] * 7, 30, 2)] == [4, 3]


def _joined(sheet: Any, role: str) -> str:
    """The texts of a role joined by spaces; the identification line's runs (its hashes are runs of their own) joined as they are within a line."""
    if role != "ident":
        return " ".join(t.text for t in sheet.texts(role))
    lines: dict[float, str] = {}
    for t in sheet.texts(role):
        lines[t.y] = lines.get(t.y, "") + t.text
    return " ".join(lines.values())


def test_printout_and_text_identify_every_page(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    import corla_observer.extract
    from corla_observer.pdfextract import Qr, Text, pdf_sheets, render_pdf

    with EventLog.open(ws.root) as log:
        built = build_extract(ws, log, "Adams", boards=1, actor=ACTOR)
    extract, sha = built.extract, built.sha256
    assert len(board_pages(extract, sha, 1)) == 1
    for name in LAYOUTS:
        monkeypatch.setitem(corla_observer.extract.LAYOUTS, name, (LAYOUTS[name][0], 8))  # several small pages
    pages = board_pages(extract, sha, 1)
    assert len(pages) > 1 and all(p.layout == "portrait-2" for p in pages)  # the default layout
    sheets = pdf_sheets(extract, sha, 1, software="corla-observer 9.9")
    assert [s.page for s in sheets] == [None] + [p.number for p in pages]
    # The provenance page comes first, with the notices, and is not counted among the pages.
    first = sheets[0]
    notices = _joined(first, "notice")
    assert notices.startswith(KEEP_OUT_OF_SIGHT) and PREDICTED_NOTE in notices
    assert [q.payload for q in first.items if isinstance(q, Qr)] == [qr_extract_payload(sha)]
    fact_text = _joined(first, "fact")
    prov = provenance(extract, sha, 1, software="corla-observer 9.9")
    for value in (SEED, grouped(sha), "corla-observer 9.9", grouped(prov.page_index_sha256), *(grouped(v) for k, v in extract["inputs"].items() if v and k != "seed")):
        assert value in fact_text
    assert all(p.sha256 not in fact_text for p in pages)  # the page hashes are in the page index and the spreadsheet
    assert "portrait-2: portrait, two columns, one page per sheet" in fact_text and f"{len(pages)} (not counting this one); " in fact_text
    steps = _joined(first, "step")
    assert all(step in steps for step in CHECK_STEPS)
    names = print_names(extract, "portrait-2")
    for page, sheet in zip(pages, sheets[1:], strict=True):
        line = page_line(extract, sha, page)
        assert _joined(sheet, "ident").replace(" \N{MIDDLE DOT} ", " ") == line.replace(" \N{MIDDLE DOT} ", " ")
        assert line == " \N{MIDDLE DOT} ".join(
            ["Adams", "Board 1", f"page {page.number} of {len(pages)} portrait-2", "PREDICTED ORDER", f"extract {sha[:8]}", f"page {page.sha256[:8]}"]
        )
        # One QR code per sheet, holding the page's hash; no seed, input hashes or notices on the pages.
        assert [q.payload for q in sheet.items if isinstance(q, Qr)] == [qr_payload(page)]
        assert not sheet.texts("notice") and not sheet.texts("fact")
        assert [t.text for t in sheet.texts("id")] == [f"ImprintedId {b['imprinted_id']}" for b in page.ballots]
        printed = [pl for b in page.ballots for pl in print_lines(b, names)]
        assert [t.text for t in sheet.texts("contest")] == [pl.contest for pl in printed if not pl.continuation]
        assert [t.text for t in sheet.texts("vote")] == [pl.value for pl in printed]
        assert len([i for i in sheet.items if getattr(i, "role", "") == "box"]) == len([pl for pl in printed if not pl.continuation])
    every = [t for s in sheets for t in s.texts("contest") + s.texts("vote")]
    assert any(t.text == "REDACTED" for t in every) and any(t.text == "NOT IN THE CVR" for t in every)
    # An overvote's choices one per line: the second on a continuation line, with no contest name and no box.
    values = [t.text for t in every]
    assert "overvote: Ann" in values and values[values.index("overvote: Ann") + 1] == "Bob"
    # Nothing printed is wider than its room.
    assert all(t.width <= t.room + 1e-6 for s in sheets for t in s.items if isinstance(t, Text) and t.room is not None)
    # Several columns: each landscape sheet is one page, with one identification line and one QR code, its ballots in columns.
    three = board_pages(extract, sha, 1, "landscape-3")
    three_sheets = pdf_sheets(extract, sha, 1, "landscape-3")
    assert len(three) < len(pages) and len(three_sheets) == len(three) + 1
    for page, sheet in zip(three, three_sheets[1:], strict=True):
        assert sheet.width > sheet.height and len([q for q in sheet.items if isinstance(q, Qr)]) == 1
        assert f"page {page.number} of {len(three)} landscape-3" in _joined(sheet, "ident")
        lefts = sorted({round(t.left) for t in sheet.texts("place")})
        assert len(lefts) == len(page.columns)
    # The PDF itself: its text as drawn.
    pdf = render_pdf(extract, sha, 1, software="corla-observer 9.9")
    assert pdf.startswith(b"%PDF-") and b"CreationDate" not in pdf and b"ModDate" not in pdf
    if shutil.which("pdftotext"):
        drawn = subprocess.run(["pdftotext", "-layout", "-", "-"], input=pdf, capture_output=True, check=True).stdout.decode("utf-8")
        assert drawn.count("\f") == len(pages) + 1 and "KEEP OUT OF SIGHT" in drawn.split("\f")[0]
        for page, text in zip(pages, drawn.split("\f")[1:], strict=False):
            assert f"page {page.sha256[:8]}" in text and "KEEP OUT OF SIGHT" not in text
    with pytest.raises(ExtractError):
        board_pages(extract, sha, 1, "sideways")
    # The plain text: one column, portrait-1's pages.
    text = render_text(extract, sha, 1, software="corla-observer 9.9")
    text_pages = board_pages(extract, sha, 1, "portrait-1")
    chunks = text.split("\f")
    assert len(chunks) == len(text_pages) + 1
    prov = provenance(extract, sha, 1, software="corla-observer 9.9", names=print_names(extract, "text"), layout="portrait-1")
    assert chunks[0] == render_provenance_text(prov)
    prov_lines = chunks[0].splitlines()
    assert all(len(line) <= 80 for line in prov_lines)
    assert " ".join(prov_lines[:9]).startswith(f"{KEEP_OUT_OF_SIGHT} {PREDICTED_NOTE}")
    text_facts = chunks[0][chunks[0].index("\n\n") + 2 : chunks[0].index("\n\nTo check")]
    facts = dict(re.split(r"\s{2,}", line, maxsplit=1) for line in re.sub(r"\n {4}(?=[0-9a-f]{8} )", "  ", re.sub(r"\n {20,}(?=\S)", " ", text_facts)).splitlines())
    assert facts["County"] == "Adams" and facts["Audit board"] == "1" and facts["Pages"].startswith(f"{len(text_pages)} ")
    assert facts["Seed"] == SEED and facts["Extract"] == grouped(sha) and facts["Software"] == "corla-observer 9.9"
    assert facts["Input: redacted CVR"] == grouped(extract["inputs"]["redacted_cvr"]) and facts["Input: pull list"] == "none"
    assert facts["Page index"] == grouped(prov.page_index_sha256) and len(facts) == 17
    assert prov.page_index_sha256 == hashlib.sha256(canonical_json(page_index(extract, sha))).hexdigest()
    for page, chunk in zip(text_pages, chunks[1:], strict=True):
        text_lines = chunk.splitlines()
        assert text_lines[0] == page_line(extract, sha, page) and text_lines[1] == ""
        assert KEEP_OUT_OF_SIGHT not in chunk
        body = text_lines[2:]
        # One line per ballot for its imprinted ID, then one per contest: the page's lines are those it was split by.
        assert len(body) == sum(block_lines(b) for b in page.ballots)
        width = max(len(c) for b in page.ballots for c, _ in print_rows(b))
        expected: list[str] = []
        for ballot in page.ballots:
            expected.append(f" ImprintedId {ballot['imprinted_id']} ".center(80, "-"))
            for c, v in print_rows(ballot):
                choices = v.removeprefix("overvote: ").split(", ") if v not in ("NO VOTE", "redacted") and ballot["status"] == "votes" else [v]
                lead = "overvote: " + choices[0] if v.startswith("overvote: ") else choices[0]
                expected.append(f"{c:>{width}} __ {lead}")
                expected.extend(f"{'':>{width}}    {more}" for more in choices[1:])
        assert body == expected
    assert "REDACTED __ redacted in the CVR" in text
    assert "Governor - DEM __ overvote: Ann\n" in text
    assert screen_line(extract, sha, 2) == f"Adams \N{MIDDLE DOT} Board 2 \N{MIDDLE DOT} extract {sha[:8]}"


def test_printed_contests_and_choices() -> None:
    assert print_contest("Representative to the 120th United States Congress - District 4 - REP") == "US Rep - CD4 - REP"
    assert print_contest("Representative to the 119th United States Congress - District 8") == "US Rep - CD8"
    assert print_contest("Regent of the University of Colorado - Congressional District 2 - DEM") == "CU Regent - CD2 - DEM"
    assert print_contest("State Board of Education Member - Congressional District 5") == "State BOE - CD5"
    assert print_contest("Amendment 79 (Constitutional)") == "Amendment 79"
    assert print_contest("Proposition JJ (Statutory)") == "Proposition JJ"
    assert print_contest("County Clerk (Vote For=1)") == "County Clerk"
    assert print_contest("Town Council (Vote For=3)") == "Town Council (Vote For=3)"
    assert print_contest("Governor - DEM") == "Governor - DEM"
    assert print_vote("no vote") == "NO VOTE"
    assert print_vote("Ann + Bob") == "Ann, Bob"
    assert print_vote("overvote: Ann + Bob") == "overvote: Ann, Bob"
    assert print_vote("redacted") == "redacted"
    assert print_vote("Ann") == "Ann"
    ballot = {"status": "votes", "votes": [["Representative to the 120th United States Congress - District 4 - REP", "no vote"]]}
    assert print_rows(ballot) == [("US Rep - CD4 - REP", "NO VOTE")]
    assert print_rows({"status": "redacted", "votes": []})[0][0] == "REDACTED"


def test_csv_escapes_formulas(ws: Workspace) -> None:
    assert [spreadsheet_cell(v) for v in ("=1+2", "+1", "-3", "@SUM(A1)", "\tx", "101-1-1", None, 5)] == [
        "'=1+2", "'+1", "'-3", "'@SUM(A1)", "'\tx", "101-1-1", "", "5"
    ]  # fmt: skip
    with EventLog.open(ws.root) as log:
        built = build_extract(ws, log, "Adams", boards=2, actor=ACTOR)
    extract = json.loads(json.dumps(built.extract))
    extract["ballots"][0]["location"] = '=HYPERLINK("http://example.org")'
    text = to_csv(extract, built.sha256)
    assert "'=HYPERLINK" in text and '\n=HYPERLINK' not in text and ",=HYPERLINK" not in text
    import csv as csv_module

    rows = list(csv_module.reader(io.StringIO(text)))
    per_layout = [c for layout in LAYOUTS for c in (f"page ({layout})", f"page hash ({layout})")]
    header = rows.index(["board", "ballot on board", "position in county", "imprinted id", "location", "status", "certain or possible", "boundary", "drawn for", "expected votes", "match", "heard instead", *per_layout])
    assert len(rows) - header - 1 == len(extract["ballots"])
    # Each ballot's page and page hash in each layout: the full list of page hashes.
    for n, layout in enumerate(LAYOUTS):
        column = 12 + 2 * n
        hashes = {(str(p.board), str(p.number)): p.sha256 for board in (1, 2) for p in board_pages(extract, built.sha256, board, layout)}
        assert all(hashes[(row[0], row[column])] == row[column + 1] for row in rows[header + 1 :])
    assert all(not cell.startswith(("=", "+", "-", "@")) for row in rows for cell in row)
    board_two = to_csv(extract, built.sha256, 2)
    assert board_two.count("\r\n") < text.count("\r\n")
    # With the printouts' hashes, a row per PDF.
    with_pdfs = to_csv(extract, built.sha256, 2, built.pdfs)
    pdf_rows = [r for r in csv_module.reader(io.StringIO(with_pdfs)) if r and r[0].startswith("PDF: ")]
    assert ["PDF: landscape-3, board 2", built.pdfs["landscape-3"]["2"]] in pdf_rows and len(pdf_rows) == len(LAYOUTS)


def test_latest_extract_and_cli(ws: Workspace, tmp_path: Path) -> None:
    runner = CliRunner()
    root = str(ws.root)
    built = runner.invoke(app, ["extract", "build", "--workspace", root, "--county", "Adams", "--actor", ACTOR, "--boards", "2"])
    assert built.exit_code == 0, built.output
    assert "PREDICTED ORDER" in built.output and "board 2:" in built.output
    with EventLog.open(ws.root, read_only=True) as log:
        found = latest_extract(ws, log, "Adams")
    assert found is not None
    shown = runner.invoke(app, ["extract", "show", "--workspace", root, "--county", "Adams", "--pages"])
    assert shown.exit_code == 0 and found[1] in shown.output and "board 1 page 1 (portrait-1):" in shown.output
    out = tmp_path / "print.pdf"
    args = ["extract", "print", "--workspace", root, "--county", "Adams", "--board", "1", "--format", "pdf", "--layout", "landscape-2", "--out", str(out)]
    printed = runner.invoke(app, args)
    assert printed.exit_code == 0, printed.output
    with EventLog.open(ws.root, read_only=True) as log:
        event: Any = [e for e in log.iter_events() if e.kind == "extract.built"][-1]
    # The PDF recorded when the extract was built, and the same bytes drawn anew.
    from corla_observer.pdfextract import render_pdf

    assert hashlib.sha256(out.read_bytes()).hexdigest() == event.data["pdfs"]["landscape-2"]["1"] in printed.output
    assert out.read_bytes() == render_pdf(found[0], found[1], 1, "landscape-2", software=event.data["software"])
    assert runner.invoke(app, args[:-2]).exit_code == 2  # a PDF needs --out
    assert runner.invoke(app, [*args[:8], "--format", "html", "--out", str(out)]).exit_code == 2
    assert runner.invoke(app, [*args[:-4], "--layout", "sideways", "--out", str(out)]).exit_code == 2
    text = runner.invoke(app, ["extract", "print", "--workspace", root, "--county", "Adams", "--board", "2"])
    assert text.exit_code == 0 and "Adams \N{MIDDLE DOT} Board 2 \N{MIDDLE DOT} page 1 of" in text.output
    assert text.output.startswith(KEEP_OUT_OF_SIGHT[:60]) and "PROVENANCE" in text.output.split("\f")[0]
    csv_out = runner.invoke(app, ["extract", "print", "--workspace", root, "--county", "Adams", "--format", "csv"])
    assert csv_out.exit_code == 0 and re.search(r"Extract,[0-9a-f]{64}", csv_out.output)
    assert f"Seed,{SEED}" in csv_out.output and re.search(r"Input: redacted CVR,[0-9a-f]{64}", csv_out.output)
    assert runner.invoke(app, ["extract", "print", "--workspace", root, "--county", "Adams"]).exit_code == 2
    assert runner.invoke(app, ["extract", "show", "--workspace", root, "--county", "Weld"]).exit_code == 1


# ---------------------------------------------------------------------------
# The web pages


def test_web_extract_pages_and_observer_identification(ws: Workspace) -> None:
    from fastapi.testclient import TestClient

    from corla_observer.practice import LinkStore
    from corla_observer.web.accounts import AccountStore
    from corla_observer.web.app import AppConfig, create_app

    with EventLog.open(ws.root) as log:
        AccountStore(ws.root).add("alice", "correct horse battery", log)
    application = create_app(AppConfig(workspace=ws.root, secret=b"s" * 32, counties=("Adams",), crawl=lambda _ws, _log: None))
    with TestClient(application, base_url="https://testserver") as client:
        assert client.get("/coord/extract", follow_redirects=False).status_code == 303
        assert client.get("/coord/extract/pdf?county=Adams&board=1", follow_redirects=False).status_code == 303
        page = client.get("/login")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert token
        client.post("/login", data={"csrf": token.group(1), "name": "alice", "password": "correct horse battery"})
        listing = client.get("/coord/extract")
        assert listing.status_code == 200 and "No RLA-sample extract for Adams yet" in listing.text and "build the extract above" in listing.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', listing.text)
        assert csrf
        bad = client.post("/coord/extract/build", data={"csrf": csrf.group(1), "county": "Weld"})
        assert bad.status_code == 400 and "no redacted CVR" in bad.text
        built = client.post("/coord/extract/build", data={"csrf": csrf.group(1), "county": "Adams", "boards": "2"})
        assert built.status_code == 200 and "Extract built" in built.text and "predicted order" in built.text
        with EventLog.open(ws.root, read_only=True) as log:
            found = latest_extract(ws, log, "Adams")
            event: Any = [e for e in log.iter_events() if e.kind == "extract.built"][-1]
        assert found is not None and event.actor == "coordinator:alice"
        extract, sha = found
        assert screen_line(extract, sha) in built.text and sha not in built.text
        assert "/coord/extract/provenance?county=Adams&amp;board=2" in built.text

        for layout in LAYOUTS:
            assert f"/coord/extract/pdf?county=Adams&amp;board=1&amp;layout={layout}" in built.text
        assert "Download PDF (landscape, two columns)" in built.text
        printed = client.get("/coord/extract/pdf?county=Adams&board=1&layout=landscape-2")
        assert printed.status_code == 200 and printed.headers["content-type"] == "application/pdf"
        assert printed.headers["content-disposition"] == f'attachment; filename="adams-board1-landscape-2-{sha[:8]}.pdf"'
        assert hashlib.sha256(printed.content).hexdigest() == event.data["pdfs"]["landscape-2"]["1"]
        assert client.get("/coord/extract/pdf?county=Adams&board=1").headers["content-disposition"].endswith(f'-board1-portrait-2-{sha[:8]}.pdf"')
        assert "Download PDF (portrait, two columns, the default)" in built.text
        assert client.get("/coord/extract/pdf?county=Adams&board=1&layout=two-up").status_code == 404
        assert client.get("/coord/extract/pdf?county=Adams&board=9").status_code == 404
        assert client.get("/coord/extract/pdf?county=Weld&board=1").status_code == 404
        assert client.get("/coord/extract/print?county=Adams&board=1").status_code == 404  # no HTML print view: the printout is the PDF
        text = client.get("/coord/extract/text?county=Adams&board=2")
        assert text.status_code == 200 and text.headers["content-type"].startswith("text/plain") and f"extract {sha[:8]}" in text.text and text.text.count("\f") == len(board_pages(extract, sha, 2))
        prov = client.get("/coord/extract/provenance?county=Adams&board=1")
        assert prov.status_code == 200 and grouped(sha) in prov.text and SEED in prov.text and "ImprintedId" not in prov.text
        assert "KEEP OUT OF SIGHT" not in prov.text and "Page index" in prov.text
        assert client.get("/coord/extract/provenance?county=Adams&board=9").status_code == 404
        sheet = client.get("/coord/extract/csv?county=Adams")
        assert sheet.status_code == 200 and "attachment" in sheet.headers["content-disposition"]
        assert sheet.content.decode("utf-8-sig").startswith("County,Adams")
        assert f"\"PDF: portrait-1, board 1\",{event.data['pdfs']['portrait-1']['1']}" in sheet.content.decode("utf-8-sig")
        assert client.get(f"/static/extract.css?v={'x'}").status_code == 200

        # The observer page loads its ballots from the committed extract and shows its identification.
        observer, _ = LinkStore(ws.root).create("observer", "ann", "Adams", 2)
        page = client.get(f"/observe/{observer}")
        assert page.status_code == 200 and screen_line(extract, sha, 2) in page.text and sha not in page.text
        assert f"/observe/{observer}/provenance" in page.text
        assert "Predicted order" in page.text
        assert f"/observe/{observer}/pdf?layout=landscape-3" in page.text
        mine = client.get(f"/observe/{observer}/pdf?layout=landscape-3")
        assert mine.status_code == 200 and hashlib.sha256(mine.content).hexdigest() == event.data["pdfs"]["landscape-3"]["2"]
        assert "attachment" in mine.headers["content-disposition"] and "-board2-landscape-3-" in mine.headers["content-disposition"]
        assert client.get(f"/observe/{observer}/pdf?layout=sideways").status_code == 404
        assert client.get(f"/read/{observer}/pdf").status_code == 404
        data = client.get(f"/observe/{observer}/data.json").json()
        assert data["extract_sha256"] == sha and data["seed"] == SEED and data["inputs"] == extract["inputs"]
        board_two = [b for b in extract["ballots"] if b["board"] == 2]
        assert [b["id"] for b in data["ballots"]] == [b["imprinted_id"] for b in board_two]
        assert [b["n"] for b in data["ballots"]] == list(range(1, len(board_two) + 1))
        # The provenance, for whoever holds the link: the seed (already in data.json) and every hash in full.
        prov = client.get(f"/observe/{observer}/provenance")
        assert prov.status_code == 200 and grouped(sha) in prov.text and SEED in prov.text and "ImprintedId" not in prov.text
        assert client.get(f"/read/{observer}/provenance").status_code == 404  # an observer's link is not a reader's
        reader, _ = LinkStore(ws.root).create("reader", "rob", "Adams", 1)
        read = client.get(f"/read/{reader}")
        assert read.status_code == 200 and screen_line(extract, sha, 1) in read.text and f"/read/{reader}/provenance" in read.text
        assert client.get(f"/read/{reader}/provenance").status_code == 200
        assert f"/read/{reader}/pdf?layout=portrait-2" in read.text
        assert hashlib.sha256(client.get(f"/read/{reader}/pdf?layout=portrait-2").content).hexdigest() == event.data["pdfs"]["portrait-2"]["1"]
        assert client.get("/observe/not-a-token/provenance").status_code == 404


def test_the_documented_verifier_agrees(ws: Workspace) -> None:
    """The Python example in docs/EXTRACT.md computes the same page hashes and page index hash as the provenance page."""
    doc = (Path(__file__).parent.parent / "docs" / "EXTRACT.md").read_text(encoding="utf-8")
    code = next(block for block in re.findall(r"```python\n(.*?)```", doc, re.S) if "def pages(" in block)
    namespace: dict[str, object] = {}
    exec(code.split("data = open")[0], namespace)
    assert namespace["LAYOUTS"] == LAYOUTS
    with EventLog.open(ws.root) as log:
        built = build_extract(ws, log, "Adams", boards=2, actor=ACTOR)
    data = ws.get_bytes(built.sha256)
    canonical = namespace["canonical"]
    assert callable(canonical) and canonical(json.loads(data)) == data
    pages = namespace["pages"]
    assert callable(pages)
    for board in (1, 2):
        for layout in LAYOUTS:
            expected = [p.sha256 for p in board_pages(built.extract, built.sha256, board, layout)]
            assert pages(json.loads(data), built.sha256, board, layout) == expected
    index = namespace["page_index_sha256"]
    assert callable(index)
    assert index(json.loads(data), built.sha256) == built.page_index_sha256 == provenance(built.extract, built.sha256, 1).page_index_sha256


# ---------------------------------------------------------------------------
# Format 2: CORLA's order, boundary ballots, certain and possible, CORLA's posting


def test_boundary_ballots_are_on_both_boards(ws: Workspace) -> None:
    with EventLog.open(ws.root) as log:
        drawn = adams_draws(ws, log)
        boards = next(b for b in (3, 4, 5, 7) if len(drawn) % b)
        built = build_extract(ws, log, "Adams", boards=boards, actor=ACTOR)
    extract = built.extract
    n = len(drawn)
    assert extract["format"] == 2 and extract["board_split"] == "uncertain"
    from corla_observer.pullorder import possible_boards

    possible = possible_boards(n, boards)
    shared = [i for i, boards in enumerate(possible) if len(boards) > 1]
    assert shared
    for i in shared:
        listed = [b for b in extract["ballots"] if b["position"] == i + 1]
        assert sorted(b["board"] for b in listed) == list(possible[i])
        assert all(b["boundary"] == [x for x in possible[i] if x != b["board"]] for b in listed)
    assert sum(extract["boards"].values()) == n + sum(len(possible[i]) - 1 for i in shared)
    # Marked on paper, in the text and in the spreadsheet; counted as a line of its own.
    boundary = next(b for b in extract["ballots"] if b["boundary"])
    assert block_lines(boundary) == 2 + max(1, len(boundary["votes"]))
    text = render_text(extract, built.sha256, boundary["board"])
    assert f"BOUNDARY: may be Board {boundary['boundary'][0]}'s ballot. If this board doesn't pull it, skip it." in text
    assert "BOUNDARY: may be" in to_csv(extract, built.sha256)
    from corla_observer.pdfextract import pdf_sheets

    marks = [t.text for s in pdf_sheets(extract, built.sha256, boundary["board"], "landscape-3") for t in s.texts("marks")]
    assert any(m.startswith("BOUNDARY: may be") for m in marks)


def test_possible_ballots_beyond_the_low_count(ws: Workspace, tmp_path: Path) -> None:
    from corla_observer.extract import make_extract
    from corla_observer.predict import latest_prediction

    with EventLog.open(ws.root) as log:
        latest = latest_prediction(ws, log)
        assert latest is not None
        prediction: Any = json.loads(json.dumps(latest[1]))
        cvr = next(e.file_sha256 for e in log.iter_events() if e.kind == "file.received")
    adams = next(c for c in prediction["contests"] if not c["statewide"])
    for contest in prediction["contests"]:
        contest["low_count"] = 2 if contest is adams else 0
    extract = make_extract(
        county="Adams", election="2026/general", seed=SEED, prediction=prediction, cvr_path=ws.path_for(str(cvr)),
        pull_list=None, boards=1, locations={}, inputs={},
    )  # fmt: skip
    certain = {b["imprinted_id"] for b in extract["ballots"] if b["certain"]}
    early = {d["imprinted_id"] for d in adams["draws"] if d["index"] <= 2}
    assert certain == early and len(certain) < len(extract["ballots"])
    possible = next(b for b in extract["ballots"] if not b["certain"])
    assert "POSSIBLE: drawn only if the sample count is above its low estimate" in render_text(extract, extract_sha256(extract), 1)
    assert block_lines(possible) == 2 + max(1, len(possible["votes"]))


def test_corla_posting_defines_the_extract_and_a_pull_list_is_checked(ws: Workspace) -> None:
    from corla_observer.flags import check_flags
    from synthetic import published_selections

    with EventLog.open(ws.root) as log:
        drawn = adams_draws(ws, log)
        posting = published_selections({"Adams County Clerk - REP": drawn[:3], "Governor - DEM": drawn[3:5]})
        fetch(ws, log, election_files(finalReports_ContestsListRound1_csv=contests_list(), finalReports_ContestIDsByCounty_csv=CONTEST_IDS) | posting)
        built = build_extract(ws, log, "Adams", boards=1, actor=ACTOR)
        assert built.extract["order"] == "audit_center" and "AUDIT CENTER ORDER" in built.extract["order_note"]
        assert sorted(b["imprinted_id"] for b in built.extract["ballots"]) == sorted(drawn[:5])
        assert built.extract["inputs"]["selection_list"] is not None
        # A pull list arriving later with other ballots: the posting stays, and the disagreement is flagged.
        pull = "Imprinted ID,Audit Board\r\n" + "".join(f"{i},Audit Board 1\r\n" for i in drawn[:4])
        receive(ws, log, "Adams", "pull_list", io.BytesIO(pull.encode()), "pull.csv", ACTOR)
        checked = build_extract(ws, log, "Adams", boards=1, actor=ACTOR)
        assert checked.extract["order"] == "audit_center" and checked.extract["checks"]["sources_disagree"]
        assert checked.extract["checks"]["other_source"] == {"source": "pull_list", "not_listed": [], "not_in_other": [drawn[4]]}
        flags = [f for f in check_flags(log, ws) if f.reason == "extract_sources_disagree"]
        assert len(flags) == 1 and flags[0].severity == "high"
        # A pull list listing exactly the posted ballots gives its own order and boards.
        pull = "Imprinted ID,Audit Board\r\n" + "".join(f"{i},Audit Board {1 + n % 2}\r\n" for n, i in enumerate(reversed(drawn[:5])))
        receive(ws, log, "Adams", "pull_list", io.BytesIO(pull.encode()), "pull2.csv", ACTOR)
        agreed = build_extract(ws, log, "Adams", actor=ACTOR)
    assert agreed.extract["order"] == "pull_list" and agreed.extract["board_split"] == "pull_list"
    assert [b["imprinted_id"] for b in agreed.extract["ballots"]] == list(reversed(drawn[:5]))
    assert not agreed.extract["checks"]["sources_disagree"]


def test_a_version_1_extract_keeps_its_page_hashes(ws: Workspace) -> None:
    from corla_observer.extract import page_format

    with EventLog.open(ws.root) as log:
        built = build_extract(ws, log, "Adams", boards=1, actor=ACTOR)
    old: dict[str, Any] = json.loads(json.dumps(built.extract))
    old["format"] = 1
    for b in old["ballots"]:
        b.pop("certain"), b.pop("boundary")
    assert page_format(old) == 2 and page_format(built.extract) == 6
    page = board_pages(old, "a" * 64, 1)[0]
    record = {"format": 2, "kind": "observer_extract_page", "extract_sha256": "a" * 64, "county": "Adams", "board": 1,
              "page": 1, "pages": page.pages, "ballots": list(page.ballots)}  # fmt: skip
    assert page.sha256 == hashlib.sha256(canonical_json(record)).hexdigest()
    assert page_index(old, "a" * 64)["page_lines"] == 70 and "layouts" not in page_index(old, "a" * 64)
    with pytest.raises(ExtractError, match="no landscape-2 layout"):
        board_pages(old, "a" * 64, 1, "landscape-2")
    # Its printout: portrait, drawn in the one-column layout.
    from corla_observer.pdfextract import pdf_sheets, render_pdf

    assert render_pdf(old, "a" * 64, 1).startswith(b"%PDF-")
    assert "page 1 of" in " ".join(t.text for t in pdf_sheets(old, "a" * 64, 1)[1].texts("ident"))


def test_unchanged_extract_is_recorded_again_when_its_pages_change(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """With skip_if_unchanged, the same extract is not recorded again, unless its page index changed (a new page format)."""
    import corla_observer.extract

    with EventLog.open(ws.root) as log:
        first = build_extract(ws, log, "Adams", boards=1, actor=ACTOR)
        again = build_extract(ws, log, "Adams", boards=1, actor=ACTOR, skip_if_unchanged=True)
        assert again.seq == first.seq
        monkeypatch.setitem(corla_observer.extract.LAYOUTS, "landscape-2", (2, 10))
        anew = build_extract(ws, log, "Adams", boards=1, actor=ACTOR, skip_if_unchanged=True)
        event: Any = [e for e in log.iter_events() if e.kind == "extract.built"][-1]
    assert anew.sha256 == first.sha256 and anew.seq > first.seq and anew.page_index_sha256 != first.page_index_sha256
    assert event.data["page_index_sha256"] == anew.page_index_sha256 and event.data["pages"]["landscape-2"]
    assert set(event.data["pdfs"]) == set(corla_observer.extract.LAYOUTS) and anew.pdfs == event.data["pdfs"]
