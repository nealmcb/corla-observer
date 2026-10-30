"""Measure how reliably the printed page's QR code decodes, for several code sizes, from simulated photos of a whole sheet.

What this is for: the QR code in each printed page's header band
(`corla_observer.printlayout.QR_SIDE`) must decode from an ordinary phone
photo of the whole sheet, not only from a close-up or a scan. This script
draws page 1 of board 1 of the synthetic extract of
``tests/photofixtures.py`` in each print layout with the QR code at each
size asked for (the rest of the sheet unchanged), rasterizes it at 300 dpi
with ``pdftoppm``, makes simulated photos of it, and counts how many
decode to the page's hash:

- **whole-sheet phone photos** at a given width in pixels (default 1600,
  1200 and 1000): the sheet on a desk, turned by up to 4 degrees, in
  perspective by up to 5 percent, blurred by 0.5 to 1.2 pixels, JPEG
  quality 50 to 80, then reduced so the whole photo is that wide; each
  trial with its own random amounts (``--trials`` per cell);
- the photo check's own degradations (``photoimages.DEGRADATIONS``, from a
  200 dpi raster), once each, with ``--degradations``.

Each image is decoded two ways: ``zbarimg`` once on the image as it is (a
plain decoder, as a phone's scanner app might be), and the photo check's
decoder (`corla_observer.photoimage.decode_qr`, which tries enlarged
corners and other preparations in turn). The sizes in docs/EXTRACT.md
were chosen with it; re-run it if the header, the code's payload or the
decoder changes.

Usage::

    uv run python scripts/qr_size_rates.py --out DIR [--size-mm 12 --size-mm 22 ...] [--layout L ...]
        [--width 1600 ...] [--trials 12] [--degradations] [--jobs 8] [--json results.json]

DIR receives the images (keep it out of the repository). Needs pdftoppm and zbarimg.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import random
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

_TESTS = Path(__file__).resolve().parent.parent / "tests"
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import photoimages  # noqa: E402
from photofixtures import realistic_extract  # noqa: E402
from PIL import Image  # noqa: E402

from corla_observer import printlayout  # noqa: E402
from corla_observer.extract import board_pages, extract_sha256, qr_payload  # noqa: E402
from corla_observer.pdfextract import draw_pdf, pdf_sheets  # noqa: E402
from corla_observer.photoimage import decode_qr  # noqa: E402

DEFAULT_SIZES = (10.0, 12.0, 16.0, 19.0, 22.0, 25.0)
DEFAULT_WIDTHS = (1600, 1200, 1000)


def phone_photo(image: Image.Image, width: int, rng: random.Random) -> Image.Image:
    """A simulated hand-held photo of the whole sheet, ``width`` pixels wide."""
    steps = photoimages.chain(
        photoimages.on_background,
        photoimages.rotated(rng.uniform(-4, 4)),
        photoimages.warped(rng.uniform(0.0, 0.05), seed=rng.randrange(1000)),
        photoimages.blurred(rng.uniform(0.5, 1.2) * image.width / width),
    )
    shot = steps(image)
    shot = shot.resize((width, round(shot.height * width / shot.width)), Image.Resampling.LANCZOS)
    return photoimages.jpeg(rng.randrange(50, 81))(shot)


def _decode(args: tuple[str, str, str]) -> tuple[bool, bool]:
    image_path, expected, work = args
    plain = subprocess.run(["zbarimg", "-q", "--raw", "-Sdisable", "-Sqrcode.enable", image_path], capture_output=True, text=True, check=False)
    plain_ok = expected in plain.stdout.split()
    folder = Path(work)
    folder.mkdir(parents=True, exist_ok=True)
    codes = decode_qr(Image.open(image_path), folder)
    return plain_ok, any(c.data == expected for c in codes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--size-mm", type=float, action="append")
    parser.add_argument("--layout", action="append", choices=list(printlayout.LAYOUTS))
    parser.add_argument("--width", type=int, action="append")
    parser.add_argument("--trials", type=int, default=12)
    parser.add_argument("--degradations", action="store_true")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    out: Path = args.out
    sizes = args.size_mm or list(DEFAULT_SIZES)
    layouts = args.layout or list(printlayout.LAYOUTS)
    widths = args.width or list(DEFAULT_WIDTHS)
    extract = realistic_extract()
    sha = extract_sha256(extract)

    jobs: list[tuple[str, float, str, str, str]] = []  # (layout, size, condition, image, expected)
    for layout in layouts:
        expected = qr_payload(board_pages(extract, sha, 1, layout)[0])
        for size in sizes:
            geometry = dataclasses.replace(printlayout.LAYOUTS[layout], qr_side=size * printlayout.MM)
            sheet = pdf_sheets(extract, sha, 1, layout, geometry=geometry)[1]
            pdf = draw_pdf([sheet], title="QR size", subject="", doc_id=b"\0" * 16)
            folder = out / layout / f"{size:g}mm"
            hi = photoimages.rasterize(pdf, folder, "sheet", dpi=300)[0]
            base = Image.open(hi).convert("L")
            rng = random.Random(f"{layout}-{size}")
            for width in widths:
                for trial in range(args.trials):
                    path = folder / f"phone-{width}-{trial}.jpg"
                    phone_photo(base, width, rng).save(path, "JPEG", quality=92)
                    jobs.append((layout, size, f"phone photo {width} px wide", str(path), expected))
            if args.degradations:
                lo = photoimages.rasterize(pdf, folder, "sheet200", dpi=200)[0]
                for condition in photoimages.DEGRADATIONS:
                    jobs.append((layout, size, condition, str(photoimages.degrade(lo, condition, folder)), expected))
    work = out / "work"
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(_decode, [(j[3], j[4], str(work / str(n))) for n, j in enumerate(jobs)]))

    table: dict[tuple[str, str, float], list[tuple[bool, bool]]] = defaultdict(list)
    for (layout, size, condition, _, _), result in zip(jobs, results, strict=True):
        table[(layout, condition, size)].append(result)
    conditions = list(dict.fromkeys(j[2] for j in jobs))
    print(f"{'layout':12} {'condition':28} " + " ".join(f"{s:>12g}mm" for s in sizes))
    print(f"{'':12} {'':28} " + " ".join(f"{'plain/check':>14}" for _ in sizes))
    for layout in layouts:
        for condition in conditions:
            cells = []
            for size in sizes:
                got = table[(layout, condition, size)]
                cells.append(f"{sum(p for p, _ in got)}/{sum(c for _, c in got)} of {len(got)}".rjust(14))
            print(f"{layout:12} {condition:28} " + " ".join(cells))
    if args.json:
        records: list[dict[str, Any]] = [
            {"layout": j[0], "size_mm": j[1], "condition": j[2], "image": Path(j[3]).name, "plain": r[0], "check": r[1]}
            for j, r in zip(jobs, results, strict=True)
        ]
        args.json.write_text(json.dumps(records, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
