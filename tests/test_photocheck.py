"""Fast tests for the photo check (corla_observer.photocheck, corla_observer.photoimage): finding pages, commitments, judging readings, photos, the command and the web pages.

The OCR tests read one committed photo-like image of a printed page
(``tests/fixtures/photo/``, made by the real print pipeline, see
``photoimages.make_photo_fixtures``) and need tesseract and zbarimg; they
are skipped where those are not installed. The heavy end-to-end tests,
which print pages as PDFs, rasterize and degrade them, are in
``test_photocheck_print.py`` (marked slow).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.events import EventLog
from corla_observer.extract import board_pages, qr_extract_payload, qr_payload
from corla_observer.photocheck import (
    ExtractRecord,
    PageFinder,
    PageLocation,
    Reading,
    Verdict,
    _judge_votes,
    align,
    check_photo,
    classify,
    commitments,
    expected_lines,
    header_id,
    ident_fields,
    lookup_short,
    near_miss,
    normal_id,
    padded,
    parse_qr,
    record_check,
    well_formed_id,
)
from corla_observer.photoimage import Line, PhotoError, Word, load_photo, strip_jpeg
from corla_observer.timestamp import OTS_BITCOIN_TAG, bitcoin_block_heights
from corla_observer.workspace import Workspace
from photofixtures import ACTOR, commit_evidence_record, realistic_extract, store_extract

FIXTURES = Path(__file__).parent / "fixtures" / "photo"
#: A photo-like image of board 1's portrait-1 page 2 of `realistic_extract()`: on a desk, slightly blurred, at 150 dpi and JPEG-compressed.
PAGE_PHOTO = FIXTURES / "weld-b1-portrait-p2.jpg"
tools = pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("zbarimg")), reason="needs tesseract and zbarimg")


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """The realistic extract, stored and committed (version 1, upgraded to a Bitcoin block), with a practice extract of another county."""
    store = Workspace.open(tmp_path / "ws")
    with EventLog.open(store.root) as log:
        sha, index = store_extract(store, log, realistic_extract())
        commit_evidence_record(store, log, {"derived/extract-Weld.json": sha, "derived/extract-Weld-pages.json": index}, bitcoin=921234)
        other = dict(realistic_extract("Adams", ballots=20, boards=1, seed=3), purpose="practice")
        store_extract(store, log, other)
    return store


def _line(text: str, conf: float = 95.0) -> Line:
    words = []
    x = 0
    for w in text.split():
        words.append(Word(w, conf, x, 10, 10 * len(w), 20))
        x += 10 * len(w) + 8
    return Line(words)


# ---------------------------------------------------------------------------
# Finding the page and its commitments


def test_qr_payloads() -> None:
    page, extract = "ab" * 32, "cd" * 32
    pages, extracts, other = parse_qr([f"COX2:{page.upper()}", f"COX2E:{extract.upper()}", "COX2:not-hex", f"COX1:{page.upper()}:{extract.upper()}", "hello"])
    assert pages == [page] and extracts == [extract] and other == ["COX2:not-hex", "hello"]


def test_page_finder_and_lookup(ws: Workspace) -> None:
    extract = realistic_extract()
    with EventLog.open(ws.root, read_only=True) as log:
        finder = PageFinder(ws, log)
        sha = next(r.sha256 for r in finder.records.values() if r.county == "Weld")
        for layout in ("portrait-1", "portrait-2", "landscape-2", "landscape-3"):
            for page in board_pages(extract, sha, 2, layout):
                found = finder.by_hash(page.sha256)
                assert found is not None and (found.layout, found.board, found.page, found.pages) == (layout, 2, page.number, page.pages)
                assert found.extract.latest and found.extract.purpose == "audit"
                assert finder.by_hash(page.sha256, counties=["Adams"]) is None  # an observer of another county can't find it
                assert finder.by_short(page.sha256[:8], sha[:8]) == [found]
        assert finder.by_hash("0" * 64) is None
        page = board_pages(extract, sha, 1)[0]
        result = lookup_short(ws, log, sha[:8], page.sha256[:8])
        assert result.error is None and len(result.pages) == 1 and result.pages[0][1][0].bitcoin_block == 921234
        assert lookup_short(ws, log, "", page.sha256[:8], counties=["Adams"]).pages == []
        assert lookup_short(ws, log, "xyz", "not hex").error


def test_superseded_extract(ws: Workspace) -> None:
    with EventLog.open(ws.root) as log:
        old = PageFinder(ws, log)
        old_sha = next(r.sha256 for r in old.records.values() if r.county == "Weld")
        store_extract(ws, log, realistic_extract(seed=8))
        finder = PageFinder(ws, log)
    page = board_pages(realistic_extract(), old_sha, 1)[0]
    found = finder.by_hash(page.sha256)
    assert found is not None and not found.extract.latest


def test_commitments_and_proofs(ws: Workspace) -> None:
    with EventLog.open(ws.root) as log:
        finder = PageFinder(ws, log)
        record = next(r for r in finder.records.values() if r.county == "Weld")
        assert record.page_index_sha256
        wanted = {"page index": record.page_index_sha256, "extract": record.sha256}
        found = commitments(ws, log, wanted)
        assert len(found) == 1 and found[0].covers == ("page index", "extract")
        assert (found[0].ots, found[0].bitcoin_block, found[0].rekor_log_index, found[0].pushed) == ("bitcoin", 921234, 4242, True)
        # A later version listing only the extract, its proof still pending.
        commit_evidence_record(ws, log, {"derived/extract-Weld.json": record.sha256}, version=2, rekor=None)
        later = commitments(ws, log, wanted)
        assert [c.version for c in later] == [1, 2] and later[1].covers == ("extract",) and later[1].ots == "pending"
        assert later[1].rekor_log_index is None and later[1].sigstore == "skipped"
        assert commitments(ws, log, {"extract": "f" * 64}) == []


def test_bitcoin_block_heights() -> None:
    # A Bitcoin attestation: its tag, then its payload's length and the block height, as varuints.
    height = 921234
    payload = bytearray()
    value = height
    while True:
        byte = value & 0x7F
        value >>= 7
        payload.append(byte | (0x80 if value else 0))
        if not value:
            break
    proof = b"\x00junk" + OTS_BITCOIN_TAG + bytes([len(payload)]) + bytes(payload)
    assert bitcoin_block_heights(proof) == [height]
    assert bitcoin_block_heights(b"pending only") == []
    assert bitcoin_block_heights(OTS_BITCOIN_TAG) == []  # truncated


# ---------------------------------------------------------------------------
# Reading and judging


def test_imprinted_id_readings() -> None:
    assert normal_id("101-7-1.", "101-7-1") == "101-7-1"
    assert normal_id("lO1-7-l", "101-7-1") == "101-7-1"  # look-alike letters, when the IDs are all digits
    assert normal_id("101-5-212.-@1@-@", "101-5-211") == "101-5-212"
    assert normal_id("AB-12", "AB-12") == "AB-12"
    assert well_formed_id("101-7-6", "101-7-1") and not well_formed_id("1017", "101-7-1") and not well_formed_id("101-7", "101-7-1")
    assert near_miss("101-4-186", "101-4-188", "id") and not near_miss("101-4-188", "101-4-188", "id") and not near_miss("102-49-88", "101-4-188", "id")
    assert near_miss("a" * 60 + "bbbb", "a" * 64, "hash") and not near_miss("b" * 64, "a" * 64, "hash")
    assert padded("1101-30-231", "101-30-231") and not padded("101-30-231", "101-30-231") and not padded("101-30-23", "101-30-231")
    assert header_id("1/45 I mprintedid 101-4-188 ———— loc Bin #1".split(), "101-4-188") == "101-4-188"
    assert header_id("2145 Imprintedld 101-5-211. ———__ loc Bin#1".split(), "101-5-211") == "101-5-211"
    assert header_id("2/45 i 101-5-211 loc Bin #1".split(), "101-5-211") == "101-5-211"  # "ImprintedId" unread, but the place on the board starts the line
    assert header_id("United States Senator - DEM [_] Karen Breslin".split()) is None
    assert classify(_line("8/45 ImprintedId 101-18-122 loc Bin # 1")).kind == "id"
    assert classify(_line("POSSIBLE: drawn only if the sample count is above its low estimate")).kind == "marks"
    assert classify(_line("Governor - DEM [_] Phil Weiser")).kind == "row"


def test_identification_line() -> None:
    fields = ident_fields("Weld + Board 1 page 3 of 54 landscape-2 - PREDICTED ORDER « extract b17f09c9 - page 27Ofb71a Bagel")
    assert fields == {
        "board": "Board 1",
        "page": "page 3 of 54 landscape-2",
        "extract": "extract b17f09c9",
        "page_hash": "page 270fb71a",
        "order": "PREDICTED ORDER",
        "county": "Weld",
    }
    assert "page_hash" not in ident_fields("Weld · Board 1 · page 1 of 4 · extract b17f09c9 · page 270fb71la")  # nine characters: not a short hash
    assert ident_fields("Weld · Board 1 · page 1 of 4 portrait-l · extract b17f09c9")["page"] == "page 1 of 4 portrait-1"
    # Printed on two lines, the identification is read as one.
    from corla_observer.photocheck import find_ident

    two = find_ident([_line("Weld · Board 12 · page 10 of 54 portrait-2 · AUDIT CENTER ORDER"), _line("extract b17f09c9 · page 270fb71a"), _line("1/45 ImprintedId 1-2-3")])
    assert two is not None and ident_fields(two.text)["page_hash"] == "page 270fb71a" and ident_fields(two.text)["board"] == "Board 12"
    # Or with only the page's hash on the second line (the extract's fitting on the first), as a real portrait-2 photo read it.
    first = _line("Weld - Board 1: page 1 of 21 portrait-2 » AUDIT CENTER ORDER - extract acbela4f fe ag")
    second = _line("page a63lfade 1 ar.")
    joined = find_ident([_line("Extract: Weld, audit board 1, portrait-2"), first, second, _line("1/45 ImprintedId 1-2-3")])
    assert joined is not None and ident_fields(joined.text)["page_hash"] == "page a631fade" and ident_fields(joined.text)["extract"] == "extract acbe1a4f"
    assert {id(w) for w in second.words} <= {id(w) for w in joined.words}
    # A complete one-line identification is not joined with the line after it.
    one = _line("Weld · Board 1 · page 1 of 4 · extract b17f09c9 · page 270fb71a")
    assert find_ident([one, _line("page 1234abcd")]) is one


def test_votes_never_turn_a_difference_into_a_match() -> None:
    def votes(*values: str | None, weight: float = 1.0) -> list[Reading]:
        return [Reading(v, v or "", "pass", weight=weight) for v in values]

    assert _judge_votes("A", votes("A", "A")) == ("matched", None)
    assert _judge_votes("A", votes("A", None)) == ("matched", None)
    assert _judge_votes("A", votes(None, None)) == ("unread", None)
    assert _judge_votes("A", votes("B")) == ("unread", "B")  # one reading is not enough to say it differs
    assert _judge_votes("A", votes("B", "B")) == ("differs", "B")
    assert _judge_votes("A", votes("B", "C")) == ("unread", "B")
    assert _judge_votes("A", votes("A", "B")) == ("unread", "B")  # readings disagree: neither matched nor differing
    assert _judge_votes("A", [*votes("A", "A"), *votes("B")]) == ("matched", None)
    assert _judge_votes("A", [*votes("B", "B"), *votes("A", weight=1.5)]) == ("unread", "B")
    assert _judge_votes("x", votes("other:jon smith", "other:jon smyth"), group=True) == ("differs", "other:jon smith")


def test_expected_lines_and_alignment() -> None:
    extract = realistic_extract()
    sha = hashlib.sha256(b"x").hexdigest()
    page = board_pages(extract, sha, 1, "landscape-3")[0]
    loc = PageLocation(ExtractRecord(sha, "Weld", "audit", 1, None, True), "landscape-3", 1, 1, page.pages, page.sha256)
    lines = expected_lines(extract, loc, page)
    idents = [e for e in lines if e.kind == "ident"]
    assert [e.text for e in idents] == ["Weld", "Board 1", f"page 1 of {page.pages} landscape-3", "PREDICTED ORDER", f"extract {sha[:8]}", f"page {page.sha256[:8]}"]
    body = [e for e in lines if e.kind != "ident"]
    assert {e.column for e in body} == {0, 1, 2}
    rows = [e for e in body if e.kind == "row" and e.contest and e.contest.startswith("Representative")]
    assert rows and all(r.printed_contest == "US Rep - CD8 - DEM" and "NO VOTE" in r.candidates for r in rows)
    # Aligning a column with what was read: order kept, an unreadable line skipped, a changed choice still paired.
    column = [e for e in body if e.column == 0][:8]
    read = []
    for e in column:
        if e.kind == "id":
            read.append(classify(_line(f"{e.n}/45 ImprintedId {e.imprinted_id} loc Bin # 1")))
        elif e.kind == "row":
            read.append(classify(_line(f"{e.printed_contest} [_] {e.printed_value}")))
        else:
            read.append(classify(_line(e.text)))
    del read[3]
    pairs = align(column, read)
    assert pairs[0] == 0 and pairs[2] == 2 and 3 not in pairs and pairs[4] == 3


# ---------------------------------------------------------------------------
# Photos


def test_load_photo_cleans_and_refuses() -> None:
    image = Image.new("RGB", (60, 40), "white")
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # camera make
    exif.get_ifd(0x8825)[2] = (39.0, 44.0, 0.0)  # GPS latitude
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif.tobytes(), comment=b"secret note")
    data = out.getvalue()
    assert b"PhoneMaker" in data
    photo = load_photo(data)
    assert photo.media_type == "image/jpeg" and b"PhoneMaker" not in photo.clean and b"secret note" not in photo.clean
    assert Image.open(io.BytesIO(photo.clean)).size == (60, 40)
    assert strip_jpeg(b"not a jpeg") is None
    # Turned by its EXIF orientation: re-encoded upright, still without metadata.
    turned = Image.Exif()
    turned[0x0112] = 6
    turned[0x010F] = "PhoneMaker"
    out = io.BytesIO()
    image.save(out, "JPEG", exif=turned.tobytes())
    photo = load_photo(out.getvalue())
    assert photo.image.size == (40, 60) and b"PhoneMaker" not in photo.clean
    png = io.BytesIO()
    image.save(png, "PNG")
    assert load_photo(png.getvalue()).media_type == "image/png"
    for bad in (b"%PDF-1.7 not an image", b"GIF89a" + b"\x00" * 20, b""):
        with pytest.raises(PhotoError):
            load_photo(bad)
    gif = io.BytesIO()
    image.save(gif, "GIF")
    with pytest.raises(PhotoError, match="not a JPEG"):
        load_photo(gif.getvalue())


def test_load_photo_reduces_a_large_photo(monkeypatch: pytest.MonkeyPatch) -> None:
    from corla_observer import photoimage

    out = io.BytesIO()
    turned = Image.Exif()
    turned[0x0112] = 6  # to be turned a quarter: 9000 x 7000 as stored, 7000 x 9000 upright
    Image.new("RGB", (9000, 7000), "white").save(out, "JPEG", quality=80, exif=turned.tobytes())
    data = out.getvalue()
    photo = load_photo(data)
    assert photo.image.size == (3733, 4800) and photoimage.MAX_CHECK_SIDE == 4800  # reduced for the check, upright
    assert photo.received_dimensions == (7000, 9000) and not photo.stored_reduced
    assert Image.open(io.BytesIO(photo.clean)).size == (7000, 9000)  # kept as received (upright, without metadata)
    # Received larger than the stored limit: the reduced image, as checked, is stored instead.
    monkeypatch.setattr(photoimage, "MAX_STORED_PHOTO_BYTES", len(data) - 1)
    reduced = load_photo(data)
    assert reduced.stored_reduced and Image.open(io.BytesIO(reduced.clean)).size == reduced.image.size == photo.image.size
    # Over the byte limit, or a header claiming more pixels than the limit (never decoded), refused.
    with pytest.raises(PhotoError, match="larger than 40 MB"):
        load_photo(b"\xff\xd8" + b"x" * photoimage.MAX_PHOTO_BYTES)
    bomb = io.BytesIO()
    Image.new("1", (15000, 14000)).save(bomb, "PNG")  # 210 megapixels, a few hundred KB
    with pytest.warns(Image.DecompressionBombWarning), pytest.raises(PhotoError, match="too many pixels"):
        load_photo(bomb.getvalue())


def test_record_check_event(ws: Workspace) -> None:
    from corla_observer.photocheck import CheckResult, LineResult

    image = io.BytesIO()
    Image.new("L", (10, 10), 255).save(image, "PNG")
    photo = load_photo(image.getvalue())
    result = CheckResult(Verdict.MISMATCH, "MISMATCH", kind="page", county="Weld", board=1, photo_sha256=hashlib.sha256(photo.clean).hexdigest(), media_type="image/png", size=len(photo.clean))
    result.lines = [LineResult("b3:c1", "row", "Governor - DEM __ Michael Bennet", "differs", "Phil Weiser", n=3, contest="Governor - DEM"), LineResult("b3:id", "id", "ImprintedId 101-7-1", "matched", n=3)]
    with EventLog.open(ws.root) as log:
        event = record_check(ws, log, result, photo, actor="observer:ann", via="observer")
        assert ws.get_bytes(event.file_sha256 or "") == photo.clean
        assert event.county == "Weld" and event.board == "1" and event.data["verdict"] == Verdict.MISMATCH
        assert event.data["differences"] == [{"kind": "row", "n": 3, "contest": "Governor - DEM"}]
        assert "Phil Weiser" not in json.dumps(event.data) and "101-7-1" not in json.dumps(event.data)  # no page content in the log
        assert event.data["counts"] == {"matched": 1, "differs": 1, "unread": 0, "marked": 0}
        unstored = record_check(ws, log, result, photo, actor="system", via="public", store=False)
        assert unstored.file_sha256 is None and unstored.data["stored"] is False
        with pytest.raises(ValueError):
            record_check(ws, log, result, photo, actor="system", via="carrier pigeon")


# ---------------------------------------------------------------------------
# Reading a real photo-like image


@tools
def test_a_photographed_page_matches(ws: Workspace) -> None:
    with EventLog.open(ws.root, read_only=True) as log:
        result = check_photo(ws, log, PAGE_PHOTO.read_bytes())
    assert result.verdict in (Verdict.MATCH, Verdict.UNREAD), result.summary
    assert result.located_by == "qr" and result.location is not None and (result.location.board, result.location.page) == (1, 2)
    counts = result.counts()
    assert counts["differs"] == 0 and counts["matched"] >= 0.9 * len(result.lines)
    assert result.committed and result.commitments[0].bitcoin_block == 921234


@tools
def test_a_page_with_another_pages_qr_code_is_a_mismatch(ws: Workspace, tmp_path: Path) -> None:
    """The fixture page with page 3's QR code pasted over its own: the code finds page 3, whose content the photo contradicts."""
    import segno

    extract = realistic_extract()
    with EventLog.open(ws.root, read_only=True) as log:
        sha = next(r.sha256 for r in PageFinder(ws, log).records.values() if r.county == "Weld")
    other = board_pages(extract, sha, 1, "portrait-1")[2]
    image = Image.open(PAGE_PHOTO).convert("L")
    qr = io.BytesIO()
    segno.make(qr_payload(other), error="m", micro=False).save(qr, kind="png", scale=4, border=2)
    code = Image.open(qr).convert("L")
    x0, y0, x1, y1 = json.loads((FIXTURES / "weld-b1-portrait-p2.json").read_text())["qr_box"]
    image.paste(code.resize((x1 - x0, y1 - y0)), (x0, y0))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=90)
    with EventLog.open(ws.root, read_only=True) as log:
        result = check_photo(ws, log, out.getvalue())
    assert result.location is not None and result.location.page == 3
    assert result.verdict == Verdict.MISMATCH, result.summary
    kinds = {line.kind for line in result.differences()}
    assert "ident" in kinds and "id" in kinds or "extra" in kinds


@tools
def test_unknown_page_and_provenance_qr(ws: Workspace, tmp_path: Path) -> None:
    import segno

    unknown = io.BytesIO()
    segno.make(f"COX2:{'AB' * 32}", error="m").save(unknown, kind="png", scale=6, border=4)
    with EventLog.open(ws.root, read_only=True) as log:
        result = check_photo(ws, log, unknown.getvalue())
        assert result.verdict == Verdict.UNKNOWN and "none of" in result.reasons[0]
        sha = next(r.sha256 for r in PageFinder(ws, log).records.values() if r.county == "Weld")
        provenance = io.BytesIO()
        segno.make(qr_extract_payload(sha), error="m").save(provenance, kind="png", scale=6, border=4)
        result = check_photo(ws, log, provenance.getvalue())
    # Only the code: the extract is found and committed, but none of the provenance sheet's facts could be read.
    assert result.kind == "provenance" and result.extract_sha256 == sha and result.committed
    assert result.verdict == Verdict.UNREAD and result.counts()["matched"] == 0


@pytest.fixture
def uncommitted(tmp_path: Path) -> Workspace:
    """The realistic extract, stored but not covered by any evidence commitment."""
    store = Workspace.open(tmp_path / "uncommitted")
    with EventLog.open(store.root) as log:
        store_extract(store, log, realistic_extract())
    return store


@tools
def test_content_verdict_and_commitment_are_separate(uncommitted: Workspace, ws: Workspace) -> None:
    """A page found in the index but not yet committed gets its content verdict all the same, with the commitment status beside it."""
    with EventLog.open(uncommitted.root) as log:
        result = check_photo(uncommitted, log, PAGE_PHOTO.read_bytes())
        assert result.verdict in (Verdict.MATCH, Verdict.UNREAD), result.summary
        assert result.commitment_status == "not_committed" and not result.committed
        assert result.commitment_text().startswith("NOT YET COMMITTED: this page is in the workspace's extract")
        assert result.summary.endswith("; NOT YET COMMITTED") and "Weld · Board 1 · page 2 of 4 portrait-1 · extract" in result.summary
        assert result.exit_code_for(False) in (0, 2) and result.exit_code_for(True) in (1, 2)
        event = record_check(uncommitted, log, result, None, actor="system", via="cli")
        assert event.data["content_verdict"] == event.data["verdict"] == result.verdict
        assert event.data["commitment_status"] == "not_committed" and event.data["commitment"] is None
    with EventLog.open(ws.root) as log:
        committed = check_photo(ws, log, PAGE_PHOTO.read_bytes())
        assert committed.commitment_status == "committed"
        assert committed.commitment_text().startswith("committed in evidence version 1 at ") and "Bitcoin block 921234" in committed.commitment_text()
        event = record_check(ws, log, committed, None, actor="system", via="cli")
        recorded = event.data["commitment"]
        assert event.data["commitment_status"] == "committed" and isinstance(recorded, dict) and recorded["bitcoin_block"] == 921234
    runner = CliRunner()
    plain = runner.invoke(app, ["photo", "check", str(PAGE_PHOTO), "--workspace", str(uncommitted.root), "--no-record"])
    assert plain.exit_code in (0, 2) and "commitment: NOT YET COMMITTED" in plain.output
    strict = runner.invoke(app, ["photo", "check", str(PAGE_PHOTO), "--workspace", str(uncommitted.root), "--no-record", "--require-committed"])
    assert strict.exit_code in (1, 2) and (plain.exit_code == 2 or strict.exit_code == 1)


@tools
def test_cli(ws: Workspace, tmp_path: Path) -> None:
    runner = CliRunner()
    bad = tmp_path / "notes.txt"
    bad.write_text("not an image")
    result = runner.invoke(app, ["photo", "check", str(PAGE_PHOTO), "--workspace", str(ws.root), "--actor", ACTOR])
    assert result.exit_code in (0, 2), result.output
    assert "which page: Weld · Board 1 · page 2 of 4 portrait-1 · extract" in result.output and "Bitcoin block 921234" in result.output and "recorded as event" in result.output
    assert "commitment: committed in evidence version 1 at" in result.output and "located by its QR code" in result.output
    with EventLog.open(ws.root, read_only=True) as log:
        checked = [e for e in log.iter_events() if e.kind == "photo.checked"]
    assert len(checked) == 1 and checked[0].actor == ACTOR and checked[0].data["via"] == "cli" and checked[0].file_sha256
    result = runner.invoke(app, ["photo", "check", str(bad), "--workspace", str(ws.root), "--json", "--no-record"])
    assert result.exit_code == 1 and json.loads(result.output)[0]["error"]
    with EventLog.open(ws.root, read_only=True) as log:
        assert len([e for e in log.iter_events() if e.kind == "photo.checked"]) == 1
    result = runner.invoke(app, ["photo", "check", str(PAGE_PHOTO), "--workspace", str(tmp_path / "nowhere")])
    assert result.exit_code == 3


# ---------------------------------------------------------------------------
# The web pages


def _client(ws: Workspace, **config: object):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from corla_observer.web.accounts import AccountStore
    from corla_observer.web.app import AppConfig, create_app

    if AccountStore(ws.root).get("alice") is None:
        with EventLog.open(ws.root) as log:
            AccountStore(ws.root).add("alice", "correct horse battery", log)
    application = create_app(AppConfig(workspace=ws.root, secret=b"s" * 32, crawl=lambda _w, _l: None, **config))  # type: ignore[arg-type]
    return TestClient(application, base_url="https://testserver")


def _login(client) -> str:  # type: ignore[no-untyped-def]
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text)
    assert token
    client.post("/login", data={"csrf": token.group(1), "name": "alice", "password": "correct horse battery"})
    page = client.get("/coord/check")
    assert page.status_code == 200 and "Check a printed page" in page.text and 'capture="environment"' in page.text
    # Two controls: the camera, and a picker for a photo already taken (no capture attribute, which on Android forces the camera).
    assert "Take a photo" in page.text and "Choose a photo or file" in page.text
    assert len(re.findall(r'<input type="file" name="photo" accept="image/\*">', page.text)) == 1
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert csrf
    return csrf.group(1)


def test_web_access_and_refusals(ws: Workspace) -> None:
    from corla_observer.practice import LinkStore

    with _client(ws) as client:
        assert client.get("/coord/check", follow_redirects=False).status_code == 303
        assert client.post("/coord/check", files={"photo": ("p.jpg", b"x", "image/jpeg")}).status_code == 403
        assert client.get("/check").status_code == 404 and client.post("/check").status_code == 404  # off by default
        assert client.get("/coord/check/photo/" + "a" * 64).status_code == 404
        csrf = _login(client)
        assert client.post("/coord/check", data={"csrf": "wrong"}, files={"photo": ("p.jpg", b"x", "image/jpeg")}).status_code == 403
        refused = client.post("/coord/check", data={"csrf": csrf}, files={"photo": ("p.jpg", b"%PDF not an image", "image/jpeg")})
        assert refused.status_code == 400 and "not a JPEG" in refused.text
        # The camera input left empty (an empty file name) and the picker used: the chosen file is the one checked.
        both = client.post("/coord/check", data={"csrf": csrf}, files=[("photo", ("", b"", "application/octet-stream")), ("photo", ("p.jpg", b"%PDF not an image", "image/jpeg"))])
        assert both.status_code == 400 and "not a JPEG" in both.text
        neither = client.post("/coord/check", data={"csrf": csrf}, files=[("photo", ("", b"", "application/octet-stream")), ("photo", ("", b"", "application/octet-stream"))])
        assert neither.status_code == 400 and "Choose a photo" in neither.text
        two = client.post("/coord/check", data={"csrf": csrf}, files=[("photo", ("a.jpg", b"x", "image/jpeg")), ("photo", ("b.jpg", b"y", "image/jpeg"))])
        assert two.status_code == 400 and "unexpected file" in two.text
        big = client.post("/coord/check", content=b"x" * (42 * 1024 * 1024), headers={"content-type": "multipart/form-data; boundary=x"})
        assert big.status_code == 413 and big.headers["content-type"].startswith("text/html")
        assert "Photo too large: 42.0 MB, limit 40 MB" in big.text and "shrink it automatically" in big.text and 'href="/coord/check"' in big.text
        # The form carries the script that shrinks a large photo before it is sent.
        assert "photoshrink.js" in client.get("/coord/check").text and client.get("/coord/check").status_code == 200
        # Typed short hashes: the page as committed, with its proofs.
        with EventLog.open(ws.root, read_only=True) as log:
            finder = PageFinder(ws, log)
            sha = next(r.sha256 for r in finder.records.values() if r.county == "Weld")
        page = board_pages(realistic_extract(), sha, 1)[1]
        found = client.get(f"/coord/check?extract={sha[:8]}&page={page.sha256[:8]}")
        assert found.status_code == 200 and page.sha256 in found.text and "Bitcoin block 921234" in found.text
        assert f"ImprintedId {page.ballots[0]['imprinted_id']}" in found.text
        observer, _ = LinkStore(ws.root).create("observer", "ann", "Adams", 1)
        mine = client.get(f"/observe/{observer}/check?extract={sha[:8]}&page={page.sha256[:8]}")
        assert mine.status_code == 200 and "No page of Adams" in mine.text and page.sha256 not in mine.text  # not their county
        assert client.get("/observe/not-a-token/check").status_code == 404
        reader, _ = LinkStore(ws.root).create("reader", "rob", "Weld", 1)
        assert client.get(f"/read/{reader}/check").status_code == 200
        assert client.get(f"/observe/{reader}/check").status_code == 404  # a reader's link is not an observer's


@tools
def test_web_check_a_photo(ws: Workspace) -> None:
    from corla_observer.practice import LinkStore

    data = PAGE_PHOTO.read_bytes()
    with _client(ws) as client:
        csrf = _login(client)
        response = client.post("/coord/check", data={"csrf": csrf}, files={"photo": ("page.jpg", data, "image/jpeg")})
        assert response.status_code == 200, response.text[:500]
        assert re.search(r"(MATCH|MATCH WITH UNREAD LINES)</p>", response.text) and "Bitcoin block 921234" in response.text
        photo = re.search(r'src="(/coord/check/photo/([0-9a-f]{64}))"', response.text)
        assert photo
        served = client.get(photo.group(1))
        assert served.status_code == 200 and served.headers["content-type"] == "image/jpeg" and served.content == ws.get_bytes(photo.group(2))
        observer, _ = LinkStore(ws.root).create("observer", "ann", "Weld", 1)
        stranger, _ = LinkStore(ws.root).create("observer", "bo", "Adams", 1)
        # A quick check (a QR code of no known page) by ann: she sees her own photo, not the coordinator's.
        import segno

        quick = io.BytesIO()
        segno.make(f"COX2:{'AB' * 32}", error="m").save(quick, kind="png", scale=6, border=4)
        mine = client.post(f"/observe/{observer}/check", files={"photo": ("code.png", quick.getvalue(), "image/png")})
        assert mine.status_code == 200 and "UNKNOWN PAGE" in mine.text
        own = re.search(r'src="(/observe/[^/]+/check/photo/([0-9a-f]{64}))"', mine.text)
        assert own and client.get(own.group(1)).status_code == 200
        assert client.get(f"/observe/{observer}/check/photo/{photo.group(2)}").status_code == 404
        assert client.get(f"/coord/check/photo/{own.group(2)}").status_code == 200
        theirs = client.post(f"/observe/{stranger}/check", files={"photo": ("page.jpg", data, "image/jpeg")})
        assert theirs.status_code == 200 and "UNKNOWN PAGE" in theirs.text and "Line by line" not in theirs.text  # not their county: nothing of the page shown
    with EventLog.open(ws.root, read_only=True) as log:
        checked = [e for e in log.iter_events() if e.kind == "photo.checked"]
    assert [(e.actor, e.data["via"]) for e in checked] == [("coordinator:alice", "coordinator"), ("observer:ann", "observer"), ("observer:bo", "observer")]
    with _client(ws, public_check=True) as client:
        public = client.post("/check", files={"photo": ("code.png", quick.getvalue(), "image/png")})
        assert public.status_code == 200 and "the photo is not kept" in public.text and "/check/photo/" not in public.text
    with EventLog.open(ws.root, read_only=True) as log:
        last = [e for e in log.iter_events() if e.kind == "photo.checked"][-1]
    assert last.data["via"] == "public" and last.file_sha256 is None and not last.data["stored"]



def test_unmatched_lines_show_what_was_read(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """A line not matched carries why and every reading; the web page shows them with a crop of the line, to whoever may see the photo."""
    from corla_observer.photocheck import CheckResult, LineResult, crop_lines, reading_record, why_not_matched
    from corla_observer.web import photos

    readings = [Reading(None, "1", "pass 1", line_box=(10, 10, 200, 40)), Reading("page 0000aaaa", "0000aaaa", "re-read", weight=1.5)]
    assert why_not_matched("unread", "page a631fade", readings) == "read as something else only once: not enough to say it differs"
    assert why_not_matched("unread", "x", []) == "nothing found where the line should be"
    assert why_not_matched("unread", "x", [Reading(None, "garble", "pass 1")]).startswith("no reading was clear enough")
    assert why_not_matched("unread", "x", [Reading("x", "x", "pass 1"), Reading("y", "y", "pass 2")]).startswith("the readings disagree")
    assert why_not_matched("matched", "x", readings) == ""
    assert [reading_record(r, "page a631fade") for r in readings] == [
        {"pass": "pass 1", "text": "1", "judged": "not usable", "value": None},
        {"pass": "re-read", "text": "0000aaaa", "judged": "other", "value": "page 0000aaaa"},
    ]
    unread = LineResult("ident:page_hash", "ident", "page a631fade", "unread", "1", reason=why_not_matched("unread", "page a631fade", readings),
                        reads=[reading_record(r, "page a631fade") for r in readings], box=(10, 10, 200, 40))  # fmt: skip
    nothing = LineResult("b1:id", "id", "ImprintedId 1-2-3", "unread", None, reason="nothing found where the line should be")
    fake = CheckResult("MATCH WITH UNREAD LINES", "", lines=[LineResult("ident:county", "ident", "Weld", "matched"), unread, nothing], photo_sha256="ab" * 32)
    crop_lines(fake, Image.new("L", (400, 100), 255))
    assert list(fake.crops) == ["ident:page_hash"] and Image.open(io.BytesIO(fake.crops["ident:page_hash"])).height == 56
    assert "reads" in fake.to_dict()["lines"][1] and fake.to_dict()["lines"][1]["reads"][0]["text"] == "1"
    monkeypatch.setattr(photos, "check_photo", lambda *a, **k: fake)
    with _client(ws) as client:
        csrf = _login(client)
        page = client.post("/coord/check", data={"csrf": csrf}, files={"photo": ("page.jpg", PAGE_PHOTO.read_bytes(), "image/jpeg")})
        assert page.status_code == 200
        assert "read: &ldquo;1&rdquo;" in page.text and "read: nothing found" in page.text and "not enough to say it differs" in page.text
        assert "Each reading (2)" in page.text and 'src="data:image/png;base64,' in page.text
        assert "img-src 'self' data:" in page.headers["content-security-policy"]
    with _client(ws, public_check=True) as client:
        public = client.post("/check", files={"photo": ("page.jpg", PAGE_PHOTO.read_bytes(), "image/jpeg")})
        assert public.status_code == 200 and "read: &ldquo;1&rdquo;" in public.text and "data:image/png" not in public.text  # the photo is not kept: no crops
        assert "data:" not in public.headers["content-security-policy"]


def test_lines_split_into_three_columns(tmp_path: Path) -> None:
    """A landscape-3 page's lines, run together across columns by tesseract, are split at the spaces near each column boundary."""
    from corla_observer.photocheck import _Reader
    from corla_observer.photoimage import Prepared

    image = Image.new("L", (3000, 2000), 255)
    reader = _Reader(Prepared(image, 0, 0.0, 1.0), tmp_path, 3)

    def at(text: str, x: int, y: int) -> list[Word]:
        words = []
        for w in text.split():
            words.append(Word(w, 95.0, x, y, 12 * len(w), 20))
            x += 12 * len(w) + 10
        return words

    joined = Line(at("Governor - DEM [_] Phil Weiser", 200, 300) + at("Governor - REP [_] Mark Baisley", 1150, 300) + at("CU Regent [_] Ann", 2150, 300))
    alone = Line(at("1/45 ImprintedId 101-1-1", 1200, 400))
    columns = reader._columns([joined, alone], None, None)
    assert [[" ".join(o.words) for o in c] for c in columns] == [
        ["Governor - DEM [_] Phil Weiser"],
        ["Governor - REP [_] Mark Baisley", "1/45 ImprintedId 101-1-1"],
        ["CU Regent [_] Ann"],
    ]


def test_a_pen_mark_over_the_choices_is_unread_not_a_difference() -> None:
    """A check mark overrunning the box into the choices is read as junk: such readings never count as another value or another line."""
    from corla_observer.photocheck import Expected, _extras, _value_vote

    row = Expected("b1:c0", "row", "State Representative - District 48 - DEM __ Dez Packard", 0, 1, 1, "1-1-1", "State Representative - District 48 - DEM",
                   "State Representative - District 48 - DEM", "Dez Packard", ("Dez Packard", "Tommy Butler", "NO VOTE"))  # fmt: skip
    assert _value_vote(row, "inet", 95.0, "reread").vote is None  # a short word: a mark read as text
    assert _value_vote(row, "Jonathan Smithers", 95.0, "reread").vote == "other:jonathan smithers"  # a name: another value
    by_key = {row.key: row}

    def extra(text: str) -> list[tuple[str, str, Any]]:
        return [(f"pass {n}", "row", classify(_line(text))) for n in (1, 2)]

    assert _extras(extra("State Representative - District 48 - DEM me Packard"), by_key, [], {}) == []  # the row, garbled
    assert _extras(extra("State Representative - District 48 - DEM [1],"), by_key, [], {}) == []  # the contest, its choice unread
    assert _extras(extra("State Representative - District 48 - DEM wpeiPackrd"), by_key, [], {}) == []  # the mark run into the choice
    found = _extras(extra("State Representative - District 48 - DEM Jonathan Smithers"), by_key, [], {})
    assert [r.status for r in found] == ["differs"]  # a line that is not on the page


@tools
def test_a_page_of_an_earlier_page_format_is_explained(tmp_path: Path) -> None:
    """A page found by its QR code in a stored page index of page format 4 ("two-up"), which this version no longer draws: UNKNOWN PAGE, saying why."""
    from corla_observer.events import canonical_json
    from corla_observer.extract import page_index

    store = Workspace.open(tmp_path / "ws")
    extract = realistic_extract()
    with EventLog.open(store.root) as log:
        stored = store.put_bytes(canonical_json(extract))
        index = page_index(extract, stored.sha256)
        old = dict(index, format=4, layouts={"two-up": index["layouts"]["portrait-1"]})
        old_sha = store.put_bytes(canonical_json(old)).sha256
        log.append("extract.built", ACTOR, county="Weld", file_sha256=stored.sha256,
                   data={"sha256": stored.sha256, "purpose": "audit", "order": "predicted", "page_index_sha256": old_sha})  # fmt: skip
        result = check_photo(store, log, PAGE_PHOTO.read_bytes())
    assert result.verdict == Verdict.UNKNOWN and "two-up" in result.summary
    assert any("earlier page format" in r for r in result.reasons)


def test_screen_qr_codes_name_a_ballot() -> None:
    """The observer screen's QR code (corla_observer.extract.screen_qr_payload) is told apart from a page's and parsed."""
    from corla_observer.extract import screen_qr_payload
    from corla_observer.photocheck import parse_qr, parse_screen_qr

    sha = "ab" * 32
    code = screen_qr_payload(sha, 2, 41)
    assert code == "COX2S:" + sha[:32].upper() + ":2:41"
    assert parse_screen_qr([code, f"COX2:{sha.upper()}"]) == [(sha[:32], 2, 41)]
    assert parse_qr([code])[:2] == ([], [])
