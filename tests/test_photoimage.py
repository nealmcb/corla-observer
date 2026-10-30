"""Fast tests for the image preparations of corla_observer.photoimage that help with real phone photos: rectification, small QR codes, screens' moiré; and for finding a page from a misread identification line."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

import photoimages
from corla_observer.events import EventLog
from corla_observer.extract import board_pages
from corla_observer.photocheck import PageFinder, find_ident, ident_fields, ident_hash_tokens
from corla_observer.photoimage import Line, Word, decode_qr, descreen, fit_angles, line_angles, moire_period, qr_windows, rectify, rotate, skew_angle, workdir
from corla_observer.workspace import Workspace
from photofixtures import commit_evidence_record, realistic_extract, store_extract

FIXTURE = Path(__file__).parent / "fixtures" / "photo" / "weld-b1-portrait-p2.jpg"
zbar = pytest.mark.skipif(not shutil.which("zbarimg"), reason="needs zbarimg")


def page() -> Image.Image:
    return Image.open(FIXTURE).convert("L")


def test_rectify_straightens_a_perspective() -> None:
    warped = photoimages.warped(0.08)(page())
    level = rotate(warped, skew_angle(warped))
    before = fit_angles(line_angles(level))
    out, spread = rectify(level)
    assert before is not None and spread >= 0.3
    after = fit_angles(line_angles(out))
    assert after is not None

    def variation(coef: tuple[float, float, float]) -> float:
        corners = [coef[0] + coef[1] * x + coef[2] * y for x in (0, out.width) for y in (0, out.height)]
        return max(corners) - min(corners)

    assert variation(after) < variation(before)
    # A flat scan is left alone.
    flat, none = rectify(page())
    assert none == 0.0 and flat.size == page().size


@zbar
def test_a_small_qr_code_is_found_in_a_window() -> None:
    small = page().resize((page().width * 3 // 4, page().height * 3 // 4), Image.Resampling.LANCZOS)
    big = Image.new("L", (2600, 1900), 200)
    big.paste(small, (900, 500))
    assert qr_windows(big)
    with workdir() as work:
        codes = decode_qr(big, work)
    assert codes and codes[0].data.startswith("COX2:")


def test_moire_is_detected_and_evened_out() -> None:
    assert moire_period(page()) is None
    screen = photoimages.moire()(page())
    period = moire_period(screen)
    assert period is not None and 14 <= period <= 18
    assert descreen(screen, period).size == screen.size


def _line(text: str) -> Line:
    return Line([Word(w, 90.0, 10 * i, 0, 9, 10) for i, w in enumerate(text.split())])


def test_identification_line_read_badly() -> None:
    fields = ident_fields(": | _ Weld · Board 1 · page 1 of 28 landscape-2 · AUDIT CENTER ORDER · extract acbeladf · page 65114148")
    assert fields["county"] == "Weld" and fields["board"] == "Board 1" and fields["page"] == "page 1 of 28 landscape-2"
    assert ident_hash_tokens("Weld - Board 1 page 1 of 28 two-uf extract acbela page 65llalé vas") == ("acbe1a", "6511a1")
    lines = [_line("Imprintedid 101-1-6"), _line("Weld - Board 1 page 1 of 28 landscape-2 extract acbeladf page 65114148"), _line("Governor - DEM Phil Weiser")]
    assert find_ident(lines) is lines[1]  # anywhere, not only among the first lines
    # Printed on two lines beside the QR code: read as one.
    two = [_line("Weld - Board 1 page 1 of 21 portrait-2 AUDIT CENTER ORDER"), _line("extract acbeladf page 65114148"), _line("Governor - DEM Phil Weiser")]
    joined = find_ident(two)
    assert joined is not None and "65114148" in joined.text and "Board 1" in joined.text
    assert find_ident([_line("Governor - DEM Phil Weiser")]) is None


def test_a_page_found_from_misread_short_hashes(tmp_path: Path) -> None:
    store = Workspace.open(tmp_path / "ws")
    extract = realistic_extract()
    with EventLog.open(store.root) as log:
        sha, index = store_extract(store, log, extract)
        commit_evidence_record(store, log, {"derived/extract-Weld.json": sha, "derived/extract-Weld-pages.json": index})
        finder = PageFinder(store, log)
    target = board_pages(extract, sha, 1, "landscape-2")[0]
    swap = {"1": "l", "4": "a", "a": "4", "0": "o", "b": "6"}

    def misread(text: str, n: int) -> str:
        out, done = [], 0
        for ch in text:
            if done < n and ch in swap:
                out.append(swap[ch])
                done += 1
            else:
                out.append(ch)
        return "".join(out)

    line = f"Weld · Board 1 · page 1 of {target.pages} landscape-2 · extract {misread(sha[:8], 2)} · page {misread(target.sha256[:8], 2)}"
    location, how = finder.by_ident(line, None)
    assert location is not None and location.sha256 == target.sha256 and "misread" in how or "exactly" in how
    # The wrong board, or hashes too far off, find nothing.
    assert finder.by_ident(line.replace("Board 1", "Board 2"), None)[0] is None
    assert finder.by_ident(f"Weld · Board 1 · page 1 of {target.pages} · extract {sha[:8]} · page 00000000", None)[0] is None
    assert finder.by_ident(line, ["Adams"])[0] is None  # not the observer's county


def test_qr_code_is_large_on_the_provenance_web_page() -> None:
    """A phone photo of the provenance page on a screen needs a bigger code than paper (the printout is a PDF, its code 22 mm)."""
    css = (Path(__file__).parent.parent / "src" / "corla_observer" / "web" / "static" / "extract.css").read_text()
    assert ".provenance .qr-extract svg { width: 36mm; height: 36mm;" in css