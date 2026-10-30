"""End-to-end tests of the photo check on pages printed by the real pipeline (slow: the printout's PDF, pdftoppm, tesseract).

Each board-1 sheet of `photofixtures.realistic_extract` is printed as its
PDF, rasterized, and degraded like a photo (`photoimages`); tampered
printouts are made by altering the PDF's drawing before it is drawn. The detection rates over every condition are measured
by ``scripts/photo_check_rates.py`` and reported in docs/PHOTO_CHECK.md;
these tests pin the essentials: genuine pages are never MISMATCH, and each
kind of tampering is MISMATCH in good conditions and never MATCH.

Run with ``uv run pytest -m slow tests/test_photocheck_print.py``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import photoimages
from corla_observer.events import EventLog
from corla_observer.extract import board_pages
from corla_observer.photocheck import Verdict, check_photo
from corla_observer.workspace import Workspace
from photofixtures import commit_evidence_record, realistic_extract, store_extract

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not (photoimages.have_print_tools() and shutil.which("tesseract") and shutil.which("zbarimg")),
        reason="needs pdftoppm, tesseract and zbarimg",
    ),
]


@pytest.fixture(scope="module")
def setup(tmp_path_factory: pytest.TempPathFactory) -> tuple[Workspace, str, Path]:
    root = tmp_path_factory.mktemp("photo")
    ws = Workspace.open(root / "ws")
    with EventLog.open(ws.root) as log:
        sha, index = store_extract(ws, log, realistic_extract())
        commit_evidence_record(ws, log, {"derived/extract-Weld.json": sha, "derived/extract-Weld-pages.json": index}, bitcoin=920000)
    return ws, sha, root


def _check(ws: Workspace, image: Path) -> tuple[str, str, int]:
    with EventLog.open(ws.root, read_only=True) as log:
        result = check_photo(ws, log, image.read_bytes())
    return result.verdict, result.summary, result.counts()["marked"]


@pytest.mark.parametrize("layout", ["portrait-1", "portrait-2", "landscape-2", "landscape-3"])
def test_genuine_pages(setup: tuple[Workspace, str, Path], layout: str) -> None:
    ws, sha, root = setup
    sheets = photoimages.render_sheets(realistic_extract(), sha, 1, layout, root / layout)
    assert len(sheets) == 1 + len(board_pages(realistic_extract(), sha, 1, layout))
    verdict, summary, _ = _check(ws, photoimages.degrade(sheets[1], "clean scan", root))
    assert verdict == Verdict.MATCH, summary
    verdict, summary, _ = _check(ws, photoimages.degrade(sheets[0], "on a desk", root))
    assert verdict in (Verdict.MATCH, Verdict.UNREAD) and "provenance" in summary, summary
    for condition in ("rotated 3 degrees", "perspective 4%", "glare", "upside down"):
        verdict, summary, _ = _check(ws, photoimages.degrade(sheets[2], condition, root))
        assert verdict in (Verdict.MATCH, Verdict.UNREAD), f"{condition}: {summary}"


@pytest.mark.parametrize("layout", ["portrait-1", "portrait-2", "landscape-2", "landscape-3"])
def test_pen_ticks_are_tolerated_and_detected(setup: tuple[Workspace, str, Path], layout: str) -> None:
    ws, sha, root = setup
    sheets = photoimages.render_sheets(realistic_extract(), sha, 1, layout, root / f"{layout}-ticks", alter=photoimages.add_ticks(3))
    verdict, summary, marked = _check(ws, photoimages.degrade(sheets[1], "on a desk", root))
    assert verdict in (Verdict.MATCH, Verdict.UNREAD), summary
    assert marked > 0


@pytest.mark.parametrize("layout", ["portrait-1", "portrait-2", "landscape-2", "landscape-3"])
def test_tampered_pages(setup: tuple[Workspace, str, Path], layout: str) -> None:
    ws, sha, root = setup
    extract = realistic_extract()
    pages = board_pages(extract, sha, 1, layout)
    votes = [v for b in pages[0].ballots for v in b["votes"]]
    governor = next(i for i, (c, v) in enumerate(votes) if c.startswith("Governor") and v == "Michael Bennet")
    cases: list[tuple[str, photoimages.Alter]] = [
        ("choice", photoimages.tamper_choice(1, governor, "Phil Weiser")),
        ("imprinted ID", photoimages.tamper_id(1, 2, str(pages[1].ballots[3]["imprinted_id"]))),
        ("QR code", photoimages.swap_qr(extract, sha, 1, layout, 1, 2)),
    ]
    for name, alter in cases:
        sheet = photoimages.render_sheets(extract, sha, 1, layout, root / f"{layout}-{name.replace(' ', '-')}", alter=alter)[1]
        verdict, summary, _ = _check(ws, photoimages.degrade(sheet, "on a desk", root))
        assert verdict == Verdict.MISMATCH, f"{name}: {summary}"
        verdict, summary, _ = _check(ws, photoimages.degrade(sheet, "phone photo (all moderate)", root))
        assert verdict != Verdict.MATCH, f"{name}, phone photo: {summary}"
