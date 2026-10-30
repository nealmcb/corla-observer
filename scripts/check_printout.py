"""Check a county's printed extract as printed: its PDFs reproduce, every sheet's QR code decodes to its hash, and no line wraps.

What this is for: the visual and mechanical check of the printout
(`corla_observer.pdfextract`) on a real extract, before relying on it. For
one board of a county's latest extract in a workspace, in each layout
(or those given), it:

1. takes the PDF recorded when the extract was built (``pdfs`` in its
   ``extract.built`` event) and draws it again, twice, and checks all three
   are the same bytes;
2. rasterizes every sheet to PNG with ``pdftoppm`` (kept in OUT, for
   looking at) and reads each with ``zbarimg``: exactly one QR code per
   sheet, the extract's hash (``COX2E:``) on the provenance sheet and the
   page's hash (``COX2:``) on each page;
3. extracts the text with ``pdftotext -layout`` and checks that every
   ballot's imprinted ID line and every contest name and choice drawn on a
   page appears whole on one line of that page's text, so that nothing was
   wrapped or cut off.

It prints each layout's PDF hash, its sheets and any problem, and exits 1
if there is one.

Usage::

    uv run python scripts/check_printout.py --workspace WS --county Weld [--board 1] [--layout L ...] --out DIR [--dpi 100]

Needs pdftoppm, pdftotext and zbarimg.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

from corla_observer.events import EventLog
from corla_observer.extract import LAYOUTS, board_pages, extract_pdfs, extract_software, latest_extract, qr_extract_payload, qr_payload
from corla_observer.pdfextract import pdf_filename, pdf_sheets, render_pdf
from corla_observer.workspace import Workspace


def check_layout(ws: Workspace, log: EventLog, county: str, board: int, layout: str, out: Path, dpi: int) -> list[str]:
    found = latest_extract(ws, log, county)
    if found is None:
        return [f"no extract for {county}"]
    extract, sha = found
    problems: list[str] = []
    software = extract_software(log, sha)
    recorded = extract_pdfs(log, sha).get(layout, {}).get(str(board))
    first = render_pdf(extract, sha, board, layout, software=software)
    second = render_pdf(extract, sha, board, layout, software=software)
    digest = hashlib.sha256(first).hexdigest()
    if first != second:
        problems.append("two drawings differ")
    if recorded is None:
        problems.append("no PDF recorded for this layout and board")
    elif recorded != digest:
        problems.append(f"the recorded PDF {recorded} differs from the one drawn now {digest}")
    path = out / pdf_filename(extract, sha, board, layout)
    path.write_bytes(first)
    stem = out / path.stem
    subprocess.run(["pdftoppm", "-r", str(dpi), "-png", str(path), str(stem)], check=True)
    images = sorted(out.glob(f"{path.stem}-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    pages = board_pages(extract, sha, board, layout)
    expected = [qr_extract_payload(sha)] + [qr_payload(p) for p in pages]
    if len(images) != len(expected):
        problems.append(f"{len(images)} sheets, expected {len(expected)} (the provenance and {len(pages)} pages)")
    for n, (image, payload) in enumerate(zip(images, expected, strict=False), start=1):
        read = subprocess.run(["zbarimg", "-q", "--raw", str(image)], capture_output=True, text=True, check=False).stdout.split()
        if read != [payload]:
            problems.append(f"sheet {n}: QR codes read {read}, expected [{payload[:14]}…]")
    text = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, check=True).stdout.split("\f")
    sheets = pdf_sheets(extract, sha, board, layout, software=software)
    for sheet, page_text in zip(sheets[1:], text[1:], strict=False):
        lines = page_text.splitlines()
        for item in (*sheet.texts("id"), *sheet.texts("contest"), *sheet.texts("vote"), *sheet.texts("marks")):
            if not any(item.text in line for line in lines):
                problems.append(f"page {sheet.page}: {item.role} {item.text!r} is not whole on one line of the text")
    print(f"{layout}: {len(images)} sheets ({len(pages)} pages and the provenance), PDF SHA-256 {digest} -> {path}")
    for problem in problems:
        print(f"  PROBLEM {problem}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--county", required=True)
    parser.add_argument("--board", type=int, default=1)
    parser.add_argument("--layout", action="append", choices=list(LAYOUTS))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=100)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    ws = Workspace.open(args.workspace)
    problems = []
    with EventLog.open(ws.root, read_only=True) as log:
        for layout in args.layout or list(LAYOUTS):
            problems += check_layout(ws, log, args.county, args.board, layout, args.out, args.dpi)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
