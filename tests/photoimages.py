"""Realistic test images of printed extract pages, for the photo check's tests and its measurement script.

`render_sheets` prints a board's extract through the real print pipeline:
the printout's PDF (`corla_observer.pdfextract.render_pdf`), rasterized by
``pdftoppm``, one PNG per sheet (the provenance sheet first), so the check
reads what is actually printed. Before drawing, the sheets can be altered
the way a forger or a pen would alter the paper (`tamper_choice`,
`tamper_id`, `swap_qr`, `add_ticks`), by changing the drawing's items
(`corla_observer.pdfextract.pdf_sheets`).

The degradations (`DEGRADATIONS`) then make a sheet look photographed:
set on a darker background, rotated, warped in perspective, blurred,
glared, reduced in resolution and JPEG-compressed, using Pillow alone.

Needs ``pdftoppm`` (poppler-utils); `have_print_tools` says whether it is installed.
"""

from __future__ import annotations

import io
import math
import random
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter

from corla_observer.extract import board_pages, qr_payload
from corla_observer.pdfextract import Box, Qr, Sheet, Stroke, pdf_sheets, render_pdf

#: An alteration of the printout's sheets, made in place before they are drawn.
Alter = Callable[[list[Sheet]], None]


def have_print_tools() -> bool:
    return shutil.which("pdftoppm") is not None


def rasterize(pdf: bytes, out: Path, name: str, dpi: int = 200) -> list[Path]:
    """Each sheet of ``pdf`` as a grey PNG at ``dpi``, in order."""
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.pdf"
    path.write_bytes(pdf)
    subprocess.run(["pdftoppm", "-r", str(dpi), "-gray", "-png", str(path), str(out / name)], check=True, capture_output=True, timeout=120)
    return sorted(out.glob(f"{name}-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))


def render_sheets(
    extract: Mapping[str, Any], sha: str, board: int, layout: str, out: Path, *, dpi: int = 200, alter: Alter | None = None, software: str | None = None
) -> list[Path]:
    """The board's printed sheets as PNG files: the provenance sheet, then each page."""
    sheets = pdf_sheets(extract, sha, board, layout, software=software)
    if alter is not None:
        alter(sheets)
    pdf = render_pdf(extract, sha, board, layout, software=software, sheets=sheets)
    return rasterize(pdf, out, f"{layout}-b{board}" + ("-altered" if alter else ""), dpi)


# ---------------------------------------------------------------------------
# Altering the printout


def _page(sheets: Sequence[Sheet], page: int) -> Sheet:
    return next(s for s in sheets if s.page == page)


def tamper_choice(page: int, nth: int, new: str) -> Alter:
    """Print ``new`` as the choices of the ``nth`` contest line (from 0) of page ``page``."""

    def alter(sheets: list[Sheet]) -> None:
        _page(sheets, page).texts("vote")[nth].text = new

    return alter


def tamper_id(page: int, nth: int, new: str) -> Alter:
    """Print ``new`` as the imprinted ID of the ``nth`` ballot (from 0) of page ``page``."""

    def alter(sheets: list[Sheet]) -> None:
        _page(sheets, page).texts("id")[nth].text = f"ImprintedId {new}"

    return alter


def swap_qr(extract: Mapping[str, Any], sha: str, board: int, layout: str, page: int, other: int) -> Alter:
    """Print page ``other``'s QR code on page ``page`` (a page whose code was pasted from another)."""
    wanted = qr_payload(board_pages(extract, sha, board, layout)[other - 1])

    def alter(sheets: list[Sheet]) -> None:
        code = next(item for item in _page(sheets, page).items if isinstance(item, Qr))
        code.payload = wanted

    return alter


def add_ticks(every: int = 3, seed: int = 1) -> Alter:
    """Draw a hand-made-looking check mark in every ``every``-th check box, overrunning it as a pen does."""
    rng = random.Random(seed)
    px = 0.75  # the marks were designed in CSS pixels

    def tick(box: Box) -> Stroke:
        x0, y0 = 2 + rng.uniform(-1, 1), 6 + rng.uniform(-1, 1)
        x1, y1 = 6 + rng.uniform(-1, 1), 11 + rng.uniform(-1, 1)
        x2, y2 = 15 + rng.uniform(-2, 2), -2 + rng.uniform(-2, 1)
        width = rng.uniform(1.2, 2.0)
        top = box.y + box.h + 3 * px
        return Stroke([(box.x + x * px, top - y * px) for x, y in ((x0, y0), (x1, y1), (x2, y2))], width * px)

    def alter(sheets: list[Sheet]) -> None:
        count = 0
        for sheet in sheets:
            for item in list(sheet.items):
                if isinstance(item, Box) and item.role == "box":
                    count += 1
                    if count % every == 0:
                        sheet.items.append(tick(item))

    return alter


# ---------------------------------------------------------------------------
# Degradations


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * y for x, y in zip(m[r], m[col], strict=True)]
    return [m[i][n] / m[i][i] for i in range(n)]


def perspective_coeffs(src: Sequence[tuple[float, float]], dst: Sequence[tuple[float, float]]) -> list[float]:
    """Pillow's PERSPECTIVE coefficients mapping output points ``dst`` to input points ``src``."""
    a: list[list[float]] = []
    b: list[float] = []
    for (x, y), (u, v) in zip(dst, src, strict=True):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        b += [u, v]
    return _solve(a, b)


def on_background(image: Image.Image, margin: float = 0.06, shade: int = 90) -> Image.Image:
    """The sheet on a darker, slightly uneven surface, as photographed on a desk."""
    w, h = image.size
    mx, my = int(w * margin), int(h * margin)
    canvas = Image.new("L", (w + 2 * mx, h + 2 * my), shade)
    noise = Image.effect_noise(canvas.size, 18).filter(ImageFilter.GaussianBlur(6))
    canvas = Image.blend(canvas, noise, 0.3)
    canvas.paste(image.convert("L"), (mx, my))
    return canvas


def rotated(degrees: float) -> Callable[[Image.Image], Image.Image]:
    return lambda im: im.rotate(degrees, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=90)


def warped(strength: float, seed: int = 3) -> Callable[[Image.Image], Image.Image]:
    """Perspective: each corner moved inwards by up to ``strength`` of the image's size, as from a phone held at an angle."""

    def apply(im: Image.Image) -> Image.Image:
        rng = random.Random(seed)
        w, h = im.size
        corners = [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]
        moved = [
            (x + (1 if x == 0 else -1) * rng.uniform(0, strength) * w, y + (1 if y == 0 else -1) * rng.uniform(0, strength) * h)
            for x, y in corners
        ]
        coeffs = perspective_coeffs(corners, moved)
        return im.transform(im.size, Image.Transform.PERSPECTIVE, coeffs, Image.Resampling.BICUBIC, fillcolor=90)

    return apply


def blurred(radius: float) -> Callable[[Image.Image], Image.Image]:
    return lambda im: im.filter(ImageFilter.GaussianBlur(radius))


def reduced(factor: float) -> Callable[[Image.Image], Image.Image]:
    return lambda im: im.resize((round(im.width * factor), round(im.height * factor)), Image.Resampling.LANCZOS)


def jpeg(quality: int) -> Callable[[Image.Image], Image.Image]:
    def apply(im: Image.Image) -> Image.Image:
        out = io.BytesIO()
        im.convert("L").save(out, "JPEG", quality=quality)
        return Image.open(io.BytesIO(out.getvalue()))

    return apply


def glare(strength: float = 0.75, where: tuple[float, float] = (0.6, 0.45), size: float = 0.35) -> Callable[[Image.Image], Image.Image]:
    """A bright spot from a lamp or window, washing out the text under it, and a shadow across the opposite corner."""

    def apply(im: Image.Image) -> Image.Image:
        g = im.convert("L")
        w, h = g.size
        r = int(min(w, h) * size)
        spot = Image.radial_gradient("L").resize((2 * r, 2 * r)).point(lambda v: int(255 * strength * max(0.0, 1 - v / 255) ** 1.5))
        mask = Image.new("L", g.size, 0)
        mask.paste(spot, (int(w * where[0]) - r, int(h * where[1]) - r))
        g = Image.composite(Image.new("L", g.size, 255), g, mask)
        shadow = Image.linear_gradient("L").rotate(45, expand=False).resize(g.size).point(lambda v: int(70 * max(0.0, (v - 150) / 105)))
        return Image.composite(Image.new("L", g.size, 0), g, shadow)

    return apply


def moire(period: float = 16.0, depth: float = 0.3, tilt: float = 0.12) -> Callable[[Image.Image], Image.Image]:
    """The banding a phone camera records off a screen: brightness rising and falling across the image with ``period`` pixels, by up to ``depth``, the bands slightly tilted."""

    def apply(im: Image.Image) -> Image.Image:
        g = im.convert("L")
        w, h = g.size
        row = [int(255 * (1 - depth * (0.5 + 0.5 * math.sin(2 * math.pi * x / period)))) for x in range(w + int(tilt * h) + 1)]
        band = Image.new("L", (len(row), 1))
        band.putdata(row)
        bands = band.resize((len(row), h), Image.Resampling.NEAREST).transform((w, h), Image.Transform.AFFINE, (1, tilt, 0, 0, 1, 0), Image.Resampling.BILINEAR)
        return ImageChops.multiply(g, bands)

    return apply


def chain(*steps: Callable[[Image.Image], Image.Image]) -> Callable[[Image.Image], Image.Image]:
    def apply(im: Image.Image) -> Image.Image:
        for step in steps:
            im = step(im)
        return im

    return apply


#: The degradations measured, by name: each a photo condition, alone and combined.
DEGRADATIONS: dict[str, Callable[[Image.Image], Image.Image]] = {
    "clean scan": lambda im: im,
    "on a desk": on_background,
    "rotated 3 degrees": chain(on_background, rotated(3)),
    "rotated -6 degrees": chain(on_background, rotated(-6)),
    "upside down": chain(on_background, rotated(180)),
    "perspective 4%": chain(on_background, warped(0.04)),
    "perspective 8%": chain(on_background, warped(0.08)),
    "blur 1px": chain(on_background, blurred(1.0)),
    "blur 2px": chain(on_background, blurred(2.0)),
    "JPEG quality 30": chain(on_background, jpeg(30)),
    "150 dpi": chain(on_background, reduced(0.75)),
    "100 dpi": chain(on_background, reduced(0.5)),
    "glare": chain(on_background, glare()),
    "photo of a screen (moiré)": chain(on_background, moire(), blurred(0.6), jpeg(70)),
    "phone photo (all moderate)": chain(on_background, rotated(2), warped(0.03), blurred(0.8), glare(0.6), reduced(0.9), jpeg(60)),
}


def degrade(path: Path, name: str, out: Path) -> Path:
    """The sheet at ``path`` degraded by ``DEGRADATIONS[name]``, saved as a JPEG (or PNG for "clean scan") in ``out``."""
    image = DEGRADATIONS[name](Image.open(path).convert("L"))
    target = out / f"{path.stem}-{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}"
    if name == "clean scan":
        target = target.with_suffix(".png")
        image.save(target)
    else:
        target = target.with_suffix(".jpg")
        image.convert("L").save(target, "JPEG", quality=88)
    return target



def make_photo_fixtures(out: Path) -> Path:
    """Make ``tests/fixtures/photo/``'s photo-like image of a printed page, and its description (with where its QR code is).

    Board 1's portrait-1 page 2 of `photofixtures.realistic_extract`, printed
    at 200 dpi (`render_sheets`), on a desk, slightly blurred, reduced to
    150 dpi and JPEG-compressed. Run again whenever the printout changes:
    ``uv run python -c "import sys; sys.path.insert(0, 'tests'); import photoimages, pathlib; photoimages.make_photo_fixtures(pathlib.Path('tests/fixtures/photo'))"``.
    """
    import json
    import tempfile

    from corla_observer import printlayout
    from corla_observer.extract import extract_sha256
    from photofixtures import realistic_extract

    extract = realistic_extract()
    sha = extract_sha256(extract)
    dpi, factor, quality = 200, 0.75, 70
    with tempfile.TemporaryDirectory() as tmp:
        sheet = render_sheets(extract, sha, 1, "portrait-1", Path(tmp), dpi=dpi)[2]
        image = Image.open(sheet).convert("L")
    width, height = image.size
    photo = chain(on_background, blurred(0.7), reduced(factor))(image)
    out.mkdir(parents=True, exist_ok=True)
    target = out / "weld-b1-portrait-p2.jpg"
    photo.save(target, "JPEG", quality=quality)
    # Where the QR code is: its 33 modules, and 2 modules of quiet zone around them.
    lay = printlayout.LAYOUTS["portrait-1"]
    scale = dpi / 72
    module = lay.qr_side / printlayout.QR_MODULES
    left = (lay.pagesize[0] - printlayout.MARGIN - lay.qr_side - 2 * module) * scale
    top = (printlayout.MARGIN - 2 * module) * scale
    side = (lay.qr_side + 4 * module) * scale
    mx, my = int(width * 0.06), int(height * 0.06)
    box = [round((mx + left) * factor), round((my + top) * factor), round((mx + left + side) * factor), round((my + top + side) * factor)]
    description = {
        "made_by": f"tests/photoimages.py: make_photo_fixtures: render_sheets(realistic_extract(), board 1, portrait-1, {dpi} dpi), sheet 3 (page 2); on_background, blurred(0.7), reduced({factor}); JPEG quality {quality}",
        "qr_box": box,
        "qr_box_is": "the QR code's 33 modules and 2 modules of quiet zone around them, for pasting a QR code made with border=2 over it",
    }
    (out / "weld-b1-portrait-p2.json").write_text(json.dumps(description, indent=1) + "\n", encoding="utf-8")
    return target
