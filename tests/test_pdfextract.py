"""Fast tests for the printed extract as a PDF (corla_observer.pdfextract): reproducible bytes, nothing overflowing, one QR code per sheet."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from corla_observer import printlayout
from corla_observer.extract import LAYOUTS, board_pages, extract_sha256, qr_extract_payload, qr_payload
from corla_observer.pdfextract import Box, Qr, Rule, Text, document_id, pdf_filename, pdf_sheets, render_pdf
from photofixtures import realistic_extract

SOFTWARE = "corla-observer 0.1.0"


def small_extract() -> dict[str, Any]:
    """A tiny extract written out in full, so that its PDF's hash depends only on the code, the fonts and ReportLab."""
    ballots = []
    votes = [
        [["Governor - DEM", "Michael Bennet"], ["State Representative - District 48 - DEM", "Kelley Anne Dennison"]],
        [["Governor - DEM", "overvote: Michael Bennet + Phil Weiser"], ["County Clerk (Vote For=1)", "no vote"]],
        [],
    ]
    for n, (imprinted, these) in enumerate(zip(("101-1-1", "101-1-2", "102-7-30"), votes, strict=True), start=1):
        ballots.append(
            {
                "position": n, "board": 1, "board_position": n, "imprinted_id": imprinted, "location": "Bin 1",
                "status": "votes" if these else "redacted", "votes": these, "picks": [["Governor - DEM", "statewide"]],
                "predicted": True, "certain": n != 2, "boundary": [],
            }
        )  # fmt: skip
    return {
        "format": 2, "kind": "observer_extract", "purpose": "audit", "election": "2026/general", "county": "Testville",
        "seed": "12345678901234567890", "order": "predicted", "order_note": "predicted", "board_split": "uncertain",
        "inputs": {"pull_list": None, "redacted_cvr": "a" * 64, "county_manifest": "b" * 64, "manifests": "c" * 64,
                   "overrides": None, "prediction": "d" * 64, "seed": "12345678901234567890", "selection_list": None, "cvr_comparison": None},
        "boards": {"1": 3},
        "contests": {"Governor - DEM": {"scope": "statewide", "counties": []}},
        "choices": {"Governor - DEM": ["Michael Bennet", "Phil Weiser"]},
        "checks": {"not_predicted": [], "predicted_not_listed": []},
        "ballots": ballots,
    }  # fmt: skip


#: The SHA-256 of `small_extract`'s PDF in each layout. A change here means the printout's bytes changed:
#: the code, the bundled fonts or ReportLab (pinned in uv.lock). If that is intended, update these and say so.
PINNED = {
    "portrait-1": "3305154a001538fe376f8c8f95b63d86a1b2a53ceeb9251dc299bff41eaf6c51",
    "portrait-2": "c9fcebdb15aa88d8ba46ec0650da9ab9f94654bcfaf40010b9a355526c82e75b",
    "landscape-2": "5d5a1356fa7f718964a76459a77f3e98898e6a1bc89318f24da167b109d80e18",
    "landscape-3": "64fafde9b7793850e1110ec63eeb64e5c3265025e631258142f15c0926cc94fb",
}


@pytest.mark.parametrize("layout", list(LAYOUTS))
def test_pinned_bytes(layout: str) -> None:
    extract = small_extract()
    pdf = render_pdf(extract, extract_sha256(extract), 1, layout, software=SOFTWARE)
    assert hashlib.sha256(pdf).hexdigest() == PINNED[layout]


def test_same_bytes_every_time_and_in_another_process(tmp_path: Path) -> None:
    extract = realistic_extract()
    sha = extract_sha256(extract)
    first = {layout: render_pdf(extract, sha, 1, layout, software=SOFTWARE) for layout in LAYOUTS}
    assert first == {layout: render_pdf(extract, sha, 1, layout, software=SOFTWARE) for layout in LAYOUTS}
    # A fresh interpreter (fonts registered anew, nothing cached) draws the same bytes.
    path = tmp_path / "extract.json"
    path.write_text(json.dumps(extract), encoding="utf-8")
    code = (
        "import json, sys; from corla_observer.pdfextract import render_pdf; from corla_observer.extract import extract_sha256; "
        "e = json.load(open(sys.argv[1])); sys.stdout.buffer.write(render_pdf(e, extract_sha256(e), 1, 'landscape-3', software=sys.argv[2]))"
    )
    other = subprocess.run([sys.executable, "-c", code, str(path), SOFTWARE], capture_output=True, check=True).stdout
    assert other == first["landscape-3"]
    # Another layout, board or software string gives other bytes.
    assert len(set(first.values())) == len(LAYOUTS)
    assert render_pdf(extract, sha, 2, "landscape-3", software=SOFTWARE) != first["landscape-3"]
    assert render_pdf(extract, sha, 1, "landscape-3", software="corla-observer 9.9") != first["landscape-3"]


def test_no_dates_and_a_document_id_from_the_content() -> None:
    extract = small_extract()
    sha = extract_sha256(extract)
    pdf = render_pdf(extract, sha, 1, "portrait-1", software=SOFTWARE)
    assert b"CreationDate" not in pdf and b"ModDate" not in pdf
    from corla_observer.events import canonical_json
    from corla_observer.extract import page_index

    index = hashlib.sha256(canonical_json(page_index(extract, sha))).hexdigest()
    ident = document_id(sha, index, "portrait-1", 1, SOFTWARE).hex().encode()
    assert re.search(rb"/ID\s*\[<" + ident + rb"><" + ident + rb">\]", pdf)
    assert b"/Title (Extract: Testville, audit board 1, portrait-1)" in pdf
    assert pdf_filename(extract, sha, 1, "landscape-3") == f"testville-board1-landscape-3-{sha[:8]}.pdf"


@pytest.mark.parametrize("layout", list(LAYOUTS))
def test_nothing_overflows_or_wraps(layout: str) -> None:
    extract = realistic_extract()
    sha = extract_sha256(extract)
    lay = printlayout.LAYOUTS[layout]
    for board in (1, 2):
        sheets = pdf_sheets(extract, sha, board, layout, software=SOFTWARE)
        pages = board_pages(extract, sha, board, layout)
        assert len(sheets) == len(pages) + 1
        for sheet in sheets:
            assert (sheet.width, sheet.height) == lay.pagesize
            for item in sheet.items:
                if isinstance(item, Text):
                    # Within its room, if it has one, and within the sheet's margins.
                    assert item.room is None or item.width <= item.room + 1e-6, (item.role, item.text)
                    assert printlayout.MARGIN - 1e-6 <= item.left and item.left + item.width <= sheet.width - printlayout.MARGIN + 1e-6, item.text
                    assert item.y >= printlayout.MARGIN - 1e-6, item.text
                elif isinstance(item, Box):
                    assert item.y >= printlayout.MARGIN - 1e-6
                elif isinstance(item, Rule):
                    assert min(item.x0, item.x1) >= printlayout.MARGIN - 1e-6 and max(item.x0, item.x1) <= sheet.width - printlayout.MARGIN + 1e-6
        # Every contest line is one Text in its cell: the contest's name right-aligned to its cell, the choices after the box.
        for page, sheet in zip(pages, sheets[1:], strict=True):
            contests = sheet.texts("contest")
            assert all(t.align == "right" for t in contests)
            cells = {round(t.x - lay.contest_box + lay.pad, 3) for t in contests}
            assert cells <= {round(lay.column_x(c), 3) for c in range(lay.columns)}
            assert len(sheet.texts("id")) == len(page.ballots)


def test_layout_geometry() -> None:
    for name, lay in printlayout.LAYOUTS.items():
        assert LAYOUTS[name] == (lay.columns, lay.column_lines)
        # Readable: an x-height at least the July 2026 printout's (Courier 7.46 pt).
        assert lay.x_height >= printlayout.MIN_X_HEIGHT
        # The QR code: modules of at least half a millimetre, the band its height and quiet zone.
        assert lay.qr_side / printlayout.QR_MODULES >= 0.5 * printlayout.MM
        assert lay.header_height == pytest.approx(lay.qr_side * (1 + 4 / 33))
        assert lay.body_top - lay.column_lines * lay.leading >= printlayout.MARGIN - 1e-6
        assert lay.body_top - (lay.column_lines + 1) * lay.leading < printlayout.MARGIN
    assert [printlayout.LAYOUTS[n].columns for n in LAYOUTS] == [2, 1, 2, 3]  # the default, portrait-2, first


@pytest.mark.skipif(not (shutil.which("pdftoppm") and shutil.which("zbarimg")), reason="needs pdftoppm and zbarimg")
@pytest.mark.parametrize("layout", ["portrait-1", "landscape-3"])
def test_one_qr_code_per_sheet_decodes_to_its_hash(layout: str, tmp_path: Path) -> None:
    extract = realistic_extract()
    sha = extract_sha256(extract)
    pdf = tmp_path / "print.pdf"
    pdf.write_bytes(render_pdf(extract, sha, 1, layout, software=SOFTWARE))
    subprocess.run(["pdftoppm", "-r", "100", "-gray", "-png", str(pdf), str(tmp_path / "sheet")], check=True)
    images = sorted(tmp_path.glob("sheet-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    expected = [qr_extract_payload(sha)] + [qr_payload(p) for p in board_pages(extract, sha, 1, layout)]
    assert len(images) == len(expected)
    for image, payload in zip(images, expected, strict=True):
        read = subprocess.run(["zbarimg", "-q", "--raw", str(image)], capture_output=True, text=True, check=False).stdout.split()
        assert read == [payload]


def test_qr_items_are_the_pages_hashes() -> None:
    extract = realistic_extract()
    sha = extract_sha256(extract)
    sheets = pdf_sheets(extract, sha, 1, "landscape-2")
    codes = [[i for i in s.items if isinstance(i, Qr)] for s in sheets]
    assert all(len(c) == 1 for c in codes)
    assert [c[0].payload for c in codes] == [qr_extract_payload(sha)] + [qr_payload(p) for p in board_pages(extract, sha, 1, "landscape-2")]
    # Top right, inside the margin, as tall as the band less its quiet zone.
    lay = printlayout.LAYOUTS["landscape-2"]
    first = codes[1][0]
    assert first.x + first.side == pytest.approx(lay.pagesize[0] - printlayout.MARGIN)
    assert first.y + first.side == pytest.approx(lay.pagesize[1] - printlayout.MARGIN)
