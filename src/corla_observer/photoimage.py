"""Reading a photograph or scan of a printed extract page: its QR code and its text.

The photo check (`corla_observer.photocheck`, docs/PHOTO_CHECK.md) uses two
system tools, run as commands so that no image library beyond Pillow is
needed in Python:

- ``zbarimg`` (the zbar-tools package) decodes QR codes;
- ``tesseract`` (tesseract-ocr, with its English data) reads the text.

Here are the steps around them:

- `load_photo`: open an uploaded image (JPEG, PNG, WebP or TIFF, recognized
  by content, not by name), refuse anything else or anything too large,
  reduce it to `MAX_CHECK_SIDE` pixels on its longer side, turn it upright
  by its EXIF orientation, and make the copy that may be kept
  (`clean_photo`), without its metadata (EXIF, GPS, camera, comments): as
  received, or the reduced image if received larger than
  `MAX_STORED_PHOTO_BYTES`.
- `decode_qr`: first the small windows most like a QR code (`qr_windows`:
  about half dark, changing often across and down), each cut out and
  enlarged, which reads a code too small to read in the whole image (a
  photo of a screen from arm's length); then zbar on a few preparations of
  the whole image in turn (as it is, grey with stretched contrast,
  flattened against glare and shadow, enlarged, reduced, each corner
  enlarged), stopping at the first that reads.
- `prepare`: put the page upright and straight for reading: a quarter turn
  by the QR code's orientation (or tesseract's orientation detection); a
  photo of a screen has its moiré evened out (`moire_period`,
  `descreen`); then the small skew that maximizes the contrast between
  text rows and the spaces between them; then `rectify`, which measures
  the line angle in tiles across the page, fits it by a plane (one per
  column of a page of several) and straightens every line with a mesh
  transform, for a page photographed at an angle; and enlarge a
  low-resolution image.
- `ocr`: tesseract's words, with their positions and confidences, grouped
  in its lines; `ocr_region` reads one small region again, enlarged.
- `column_gaps`: where the columns of a page of several columns divide.
- `ink`: how dark a region is, for detecting a hand-made check mark.

Temporary files go in a directory the caller chooses (the web app uses the
workspace's staging directory, so a photo of a page is never written
anywhere outside the workspace).
"""

from __future__ import annotations

import cmath
import bisect
import io
import math
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageMath, ImageOps, UnidentifiedImageError

#: The largest photo accepted, in bytes and in pixels (a phone's full-resolution photo is 50 to 200 megapixels, 15 to 40 MB).
MAX_PHOTO_BYTES = 40 * 1024 * 1024
MAX_PHOTO_PIXELS = 200_000_000
#: A photo whose longer side is longer than this is reduced to it (Lanczos) before it is checked: more than enough for the reading, and much quicker.
MAX_CHECK_SIDE = 4800
#: A photo received larger than this is stored reduced, as it was checked, instead of as received.
MAX_STORED_PHOTO_BYTES = 15 * 1024 * 1024
#: The media types accepted, by Pillow's format name.
FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "TIFF": "image/tiff"}
#: How long one tool run may take, in seconds.
TOOL_TIMEOUT = 120

#: The width a page is enlarged to, at least, before it is read: about 300 dots per inch across a letter-size sheet, portrait or landscape.
READ_WIDTH = {"portrait": 2600, "landscape": 3300}


class PhotoError(ValueError):
    """The image cannot be used: not an accepted image type, too large, or unreadable."""


class ToolMissingError(RuntimeError):
    """A system tool the photo check needs is not installed."""


TOOLS = {"zbarimg": "zbar-tools", "tesseract": "tesseract-ocr (with tesseract-ocr-eng)"}


def tool(name: str) -> str:
    """The path of a needed system tool, or `ToolMissingError` naming the package that provides it."""
    found = shutil.which(name)
    if found is None:
        raise ToolMissingError(f"the {name} command is not installed (package {TOOLS.get(name, name)})")
    return found


def missing_tools() -> list[str]:
    """The needed system tools that are not installed, each with its package."""
    return [f"{name} ({package})" for name, package in TOOLS.items() if shutil.which(name) is None]


@contextmanager
def workdir(parent: Path | None = None) -> Iterator[Path]:
    """A private temporary directory (mode 0700) under ``parent`` (default: the system's), removed afterwards."""
    if parent is not None:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="photo-", dir=parent))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Loading and cleaning


@dataclass(frozen=True)
class Photo:
    """An accepted photo: the upright image to read, and the clean bytes that may be kept."""

    image: Image.Image
    clean: bytes
    media_type: str
    #: The SHA-256 of the clean bytes is the stored photo's identity; the original's is kept for the record.
    original_size: int
    #: The upright photo's width and height as received (``image`` may be reduced, `MAX_CHECK_SIDE`).
    received_dimensions: tuple[int, int] = (0, 0)
    #: True if ``clean`` is the reduced image as checked, not the photo as received (received larger than `MAX_STORED_PHOTO_BYTES`).
    stored_reduced: bool = False


def too_large_message(size: int | None) -> str:
    """What to tell someone whose photo is larger than `MAX_PHOTO_BYTES` (``size`` in bytes, if known)."""
    limit = MAX_PHOTO_BYTES // (1024 * 1024)
    got = f"{size / (1024 * 1024):.1f} MB" if size is not None else f"over {limit} MB"
    return f"Photo too large: {got}, limit {limit} MB. Try again: your browser should shrink it automatically (or send a smaller photo)."


_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def load_photo(data: bytes) -> Photo:
    """Open ``data`` as a photo; see the module documentation.

    The pixel count is checked from the header, before any of the image is
    decoded, and Pillow's own decompression-bomb limit is set to the same
    count meanwhile, so that no image larger than `MAX_PHOTO_PIXELS` is ever
    decoded. A photo whose longer side exceeds `MAX_CHECK_SIDE` is reduced
    to it (Lanczos) for the check; it is stored as received (without
    metadata) unless received larger than `MAX_STORED_PHOTO_BYTES`, when the
    reduced image is stored instead (`Photo.stored_reduced`).

    Raises:
        PhotoError: not JPEG, PNG, WebP or TIFF; too large; or unreadable.
    """
    if len(data) > MAX_PHOTO_BYTES:
        raise PhotoError(f"the image is larger than {MAX_PHOTO_BYTES // (1024 * 1024)} MB")
    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_PHOTO_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as probe:
            kind = probe.format or ""
            if kind not in FORMATS:
                raise PhotoError("not a JPEG, PNG, WebP or TIFF image")
            if probe.width * probe.height > MAX_PHOTO_PIXELS:
                raise PhotoError("the image has too many pixels")
            orientation = probe.getexif().get(0x0112, 1)
            probe.load()
            full = probe.convert("RGB") if probe.mode not in ("RGB", "L") else probe.copy()
    except PhotoError:
        raise
    except UnidentifiedImageError:
        raise PhotoError("not a JPEG, PNG, WebP or TIFF image") from None
    except (Image.DecompressionBombError, OSError, ValueError, SyntaxError) as exc:
        raise PhotoError(f"the image could not be read ({type(exc).__name__})") from None
    finally:
        Image.MAX_IMAGE_PIXELS = previous
    transpose = _TRANSPOSE.get(orientation) if isinstance(orientation, int) else None
    image = full
    longest = max(full.size)
    if longest > MAX_CHECK_SIDE:
        factor = MAX_CHECK_SIDE / longest
        image = full.resize((max(1, round(full.width * factor)), max(1, round(full.height * factor))), Image.Resampling.LANCZOS)
    if transpose is not None:
        image = image.transpose(transpose)
    received = (full.height, full.width) if transpose in (Image.Transpose.ROTATE_90, Image.Transpose.ROTATE_270, Image.Transpose.TRANSPOSE, Image.Transpose.TRANSVERSE) else full.size
    if len(data) > MAX_STORED_PHOTO_BYTES:
        del full
        out = io.BytesIO()
        image.convert("RGB").save(out, "JPEG", quality=92)
        return Photo(image, out.getvalue(), "image/jpeg", len(data), received, stored_reduced=True)
    upright = full.transpose(transpose) if transpose is not None else full
    clean, media_type = clean_photo(data, upright, kind)
    return Photo(image, clean, media_type, len(data), received)


_JPEG_KEEP = {0xC0, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCC, 0xCD, 0xCE, 0xCF, 0xDB, 0xDD, 0xE0}


def strip_jpeg(data: bytes) -> bytes | None:
    """A JPEG without its metadata segments (APP1 to APP15, comments), its image data untouched; None if it can't be parsed."""
    if data[:2] != b"\xff\xd8":
        return None
    out = bytearray(b"\xff\xd8")
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0xDA:  # start of scan: the rest is image data
            out += data[i:]
            return bytes(out)
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if length < 2 or i + 2 + length > len(data):
            return None
        if marker in _JPEG_KEEP:
            out += data[i : i + 2 + length]
        i += 2 + length
    return None


def clean_photo(data: bytes, image: Image.Image, kind: str) -> tuple[bytes, str]:
    """The bytes that may be kept: a JPEG losslessly without its metadata if already upright, else the upright image re-encoded without metadata."""
    if kind == "JPEG":
        try:
            with Image.open(io.BytesIO(data)) as probe:
                orientation = probe.getexif().get(0x0112, 1)
        except OSError:
            orientation = 1
        if orientation in (None, 1):
            stripped = strip_jpeg(data)
            if stripped is not None:
                return stripped, "image/jpeg"
        out = io.BytesIO()
        image.convert("RGB").save(out, "JPEG", quality=92)
        return out.getvalue(), "image/jpeg"
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue(), "image/png"


# ---------------------------------------------------------------------------
# Preparations


def grey(image: Image.Image) -> Image.Image:
    return image.convert("L") if image.mode != "L" else image


def stretch(image: Image.Image) -> Image.Image:
    """Grey, with its contrast stretched (ignoring the darkest and lightest 1 percent)."""
    return ImageOps.autocontrast(grey(image), cutoff=1)


def flatten(image: Image.Image) -> Image.Image:
    """Grey, divided by its own heavily blurred background, so that glare, shadows and uneven light even out; then stretched."""
    g = grey(image)
    small = g.resize((max(1, g.width // 8), max(1, g.height // 8)), Image.Resampling.BILINEAR)
    # A light "closing" first, so that the background is the paper, not the ink.
    background = small.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(6)).resize(g.size, Image.Resampling.BILINEAR)
    result = ImageMath.lambda_eval(lambda a: a["convert"](a["min"](a["a"] * 235 / a["max"](a["b"], 1), 255), "L"), a=g, b=background)
    assert isinstance(result, Image.Image)
    return ImageOps.autocontrast(result, cutoff=0.5)


#: A photo is of a screen if the brightness of its paper (the text taken out) rises and falls across it with one period this many times more strongly than with a typical other period.
MOIRE_RATIO = 20.0
#: The periods searched for moiré, in pixels of the photo.
MOIRE_PERIODS = (6.0, 28.0)
#: The thin horizontal strips whose profiles are summed, and their height in pixels.
MOIRE_STRIPS = (12, 24)


def _spectrum(values: list[int], lo: float, hi: float) -> dict[int, float]:
    """The power of a detrended profile at each frequency whose period (in samples) is between ``lo`` and ``hi``."""
    n = len(values)
    prefix = [0.0]
    for v in values:
        prefix.append(prefix[-1] + v)
    k = 30
    detrended = [values[i] - (prefix[min(n, i + k + 1)] - prefix[max(0, i - k)]) / (min(n, i + k + 1) - max(0, i - k)) for i in range(n)]
    found = {}
    for f in range(1, n // 2):
        if lo <= n / f <= hi:
            found[f] = abs(sum(d * cmath.exp(-2j * math.pi * f * i / n) for i, d in enumerate(detrended))) ** 2
    return found


def moire_period(image: Image.Image) -> float | None:
    """The period, in pixels, of a photo's moiré (the banding a phone camera records off a screen), or None if there is none.

    The text is taken out (each pixel the lightest of its neighbours), and
    in each of a dozen thin strips across the middle of the image the
    brightness profile across the page is analysed for periodic banding
    (`MOIRE_PERIODS`). The strips' power spectra are added: the moiré, the
    same in every strip even when its bands are slightly tilted, adds up,
    while what text leaves behind differs from strip to strip and does not.
    It is moiré when one period stands `MOIRE_RATIO` times above the median.
    """
    g = grey(image)
    w, h = g.size
    count, height = MOIRE_STRIPS
    if h < 4 * height or w < 200:
        return None
    paper = g.filter(ImageFilter.MaxFilter(5))
    region = int(w * 0.6)
    x0 = (w - region) // 2
    samples = min(768, region)
    scale = region / samples
    total: dict[int, float] = {}
    for n in range(count):
        y = int(h * 0.2 + (h * 0.6 - height) * n / (count - 1))
        profile = pixels(paper.crop((x0, y, x0 + region, y + height)).resize((samples, 1), Image.Resampling.BOX))
        for f, power in _spectrum(profile, MOIRE_PERIODS[0] / scale, MOIRE_PERIODS[1] / scale).items():
            total[f] = total.get(f, 0.0) + power
    if len(total) < 5:
        return None
    ranked = sorted(total.values())
    median = ranked[len(ranked) // 2] or 1e-9
    best = max(total, key=lambda f: total[f])
    if total[best] / median < MOIRE_RATIO:
        return None
    return samples / best * scale


def descreen(image: Image.Image, period: float) -> Image.Image:
    """Grey, with a screen's moiré of the given period evened out.

    The bands run nearly down the page, so the paper's brightness is
    estimated where it varies least with the text: each pixel the lightest
    of its close neighbours (which removes the thin strokes of the text but
    keeps the bands, which are wider), averaged down a short stretch of its
    column (less than the bands' slight tilt can move them by much); the
    image is divided by it, so that every band becomes paper white.
    """
    g = grey(image)
    paper = g.filter(ImageFilter.MaxFilter(5))
    stretch_rows = max(2, round(period * 1.5))
    small = paper.resize((g.width, max(1, g.height // stretch_rows)), Image.Resampling.BOX)
    background = small.resize(g.size, Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(1.0))
    result = ImageMath.lambda_eval(lambda a: a["convert"](a["min"](a["a"] * 235 / a["max"](a["b"], 1), 255), "L"), a=g, b=background)
    assert isinstance(result, Image.Image)
    return ImageOps.autocontrast(result.filter(ImageFilter.GaussianBlur(0.6)), cutoff=0.5)


def binarize(image: Image.Image, threshold: int = 150) -> Image.Image:
    """Black and white (as a grey image), from a flattened image."""
    return flatten(image).point(lambda v: 255 if v > threshold else 0)


def remove_rules(image: Image.Image) -> Image.Image:
    """Grey, with long horizontal lines (the rules either side of each imprinted ID) turned white, so that they do not run into the text.

    A pixel is part of a rule if the row it is in stays dark when averaged
    over a stretch a twenty-fifth of the page wide, which text never does.
    """
    g = grey(image)
    k = max(16, g.width // 25)
    small = g.resize((max(1, g.width // k), g.height), Image.Resampling.BOX)
    mask = small.point(lambda v: 255 if v < 80 else 0).resize(g.size, Image.Resampling.NEAREST).filter(ImageFilter.MaxFilter(3))
    return Image.composite(Image.new("L", g.size, 255), g, mask)


def scaled(image: Image.Image, factor: float) -> Image.Image:
    if factor == 1:
        return image
    size = (max(1, round(image.width * factor)), max(1, round(image.height * factor)))
    return image.resize(size, Image.Resampling.LANCZOS if factor < 1 else Image.Resampling.BICUBIC)


def pixels(image: Image.Image) -> list[int]:
    """A single-band image's pixel values, row by row."""
    flattened = getattr(image, "get_flattened_data", None)
    values: Iterable[Any] = flattened() if flattened is not None else image.getdata()  # getdata: Pillow before 12.1
    return [int(v) for v in values]


def _row_contrast(image: Image.Image) -> float:
    """How strongly the image's rows alternate between dark and light: high when its text lines are level."""
    rows = pixels(image.resize((1, image.height), Image.Resampling.BOX))
    return float(sum((int(a) - int(b)) ** 2 for a, b in zip(rows, rows[1:])))


def skew_angle(image: Image.Image, limit: float = 8.0) -> float:
    """The small rotation, in degrees counter-clockwise, that best levels the text lines (projection profile)."""
    g = stretch(image)
    factor = min(1.0, 900 / max(1, g.width))
    small = ImageOps.invert(scaled(g, factor).point(lambda v: 255 if v > 160 else 0))

    def score(angle: float) -> float:
        return _row_contrast(small.rotate(angle, resample=Image.Resampling.BILINEAR, fillcolor=0))

    best = max((a / 2 for a in range(int(-limit * 2), int(limit * 2) + 1)), key=score)
    return max((best + a / 10 for a in range(-5, 6)), key=score)


#: The grid of tiles whose text-line angles `line_angles` measures, across and down.
ANGLE_TILES = (6, 5)
#: A tile's angle counts only if its best angle separates the lines this many times better than a typical angle does.
ANGLE_CONFIDENCE = 3.0
#: Rectify only when the fitted line angle varies across the page by at least this much, in degrees.
RECTIFY_MIN_SPREAD = 0.3


@dataclass(frozen=True)
class TileAngle:
    """The text-line angle measured in one tile: its centre, the angle (degrees counter-clockwise that levels it) and how clear it is."""

    x: float
    y: float
    angle: float
    confidence: float


def line_angles(image: Image.Image, tiles: tuple[int, int] = ANGLE_TILES, limit: float = 10.0) -> list[TileAngle]:
    """The text-line angle in each tile of a grid over the image (projection profile, as `skew_angle`).

    A photo at an angle to the page, or of a screen seen from one side,
    has lines that slope differently in different parts of the page; this
    measures how.
    """
    g = stretch(image)
    factor = min(1.0, 1800 / max(1, g.width))
    g = scaled(g, factor)
    nx, ny = tiles
    width, height = g.size
    found = []
    for j in range(ny):
        for i in range(nx):
            box = (i * width // nx, j * height // ny, (i + 1) * width // nx, (j + 1) * height // ny)
            tile = ImageOps.invert(g.crop(box).point(lambda v: 255 if v > 160 else 0))

            def score(angle: float, tile: Image.Image = tile) -> float:
                return _row_contrast(tile.rotate(angle, resample=Image.Resampling.BILINEAR, fillcolor=0))

            coarse = {a / 2: score(a / 2) for a in range(int(-limit * 2), int(limit * 2) + 1)}
            best = max(coarse, key=lambda a: coarse[a])
            best = max((best + a / 10 for a in range(-5, 6)), key=score)
            typical = sorted(coarse.values())[len(coarse) // 2]
            confidence = score(best) / typical if typical > 0 else 0.0
            found.append(TileAngle((box[0] + box[2]) / 2 / factor, (box[1] + box[3]) / 2 / factor, best, confidence))
    return found


def _solve3(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    rows = [row[:] + [v] for row, v in zip(matrix, vector, strict=True)]
    for i in range(3):
        pivot = max(range(i, 3), key=lambda k: abs(rows[k][i]))
        if abs(rows[pivot][i]) < 1e-12:
            return None
        rows[i], rows[pivot] = rows[pivot], rows[i]
        for k in range(3):
            if k != i:
                f = rows[k][i] / rows[i][i]
                rows[k] = [a - f * b for a, b in zip(rows[k], rows[i], strict=True)]
    return [rows[i][3] / rows[i][i] for i in range(3)]


def fit_angles(angles: Iterable[TileAngle], min_tiles: int = 6) -> tuple[float, float, float] | None:
    """The plane ``angle = a + b·x + c·y`` best fitting the clear tiles' angles (weighted by clarity, outliers dropped), or None with too few."""
    points = [t for t in angles if t.confidence >= ANGLE_CONFIDENCE]
    coef: list[float] | None = None
    for _ in range(4):
        if len(points) < min_tiles:
            return None
        matrix = [[0.0] * 3 for _ in range(3)]
        vector = [0.0] * 3
        for t in points:
            row = (1.0, t.x, t.y)
            w = min(t.confidence, 30.0)
            for i in range(3):
                vector[i] += w * row[i] * t.angle
                for k in range(3):
                    matrix[i][k] += w * row[i] * row[k]
        coef = _solve3(matrix, vector)
        if coef is None:
            return None
        kept = [t for t in points if abs(t.angle - (coef[0] + coef[1] * t.x + coef[2] * t.y)) <= 0.8]
        if len(kept) == len(points):
            break
        points = kept
    return (coef[0], coef[1], coef[2]) if coef is not None else None


def rectify(image: Image.Image, columns: int = 1) -> tuple[Image.Image, float]:
    """The image with its text lines made level everywhere, not just on average; and the spread of the angles corrected (degrees).

    The text-line angle is measured in tiles (`line_angles`) and fitted by a
    plane over the page, or for a page of several columns by one plane
    per column, split at the white gaps between them (`column_gaps`). Where
    the fitted angle varies by at least `RECTIFY_MIN_SPREAD` (perspective: a
    page photographed at an angle, or a screen seen from one side), each
    line is straightened by a mesh transform that moves every point up or
    down by the slope integrated along its line from the page's centre, so
    the page is never torn at the gap. Letters are not unslanted, which OCR
    tolerates.
    """
    g = grey(image)
    width, height = g.size
    angles = line_angles(g)
    gaps: list[int] = []
    if columns >= 2:
        gaps = column_gaps(g, 0, columns) or [width * k // columns for k in range(1, columns)]
    edges = [0, *gaps, width]
    whole = fit_angles(angles)
    if whole is None:
        return image, 0.0
    fits = [whole]
    if gaps:
        fits = [fit_angles([t for t in angles if lo <= t.x < hi], min_tiles=4) or whole for lo, hi in zip(edges, edges[1:])]

    def angle(x: float, y: float) -> float:
        coef = fits[bisect.bisect_right(gaps, x)] if gaps else whole
        return coef[0] + coef[1] * x + coef[2] * y

    corners = [angle(x, y) for lo, hi in zip(edges, edges[1:]) for x in (lo, max(lo, hi - 1)) for y in (0, height)]
    spread = max(max(corners) - min(corners), max(abs(c) for c in corners))
    if spread < RECTIFY_MIN_SPREAD:
        return image, 0.0
    centre = width / 2
    nx, ny = 12, 12
    xs = [round(width * i / nx) for i in range(nx + 1)]
    ys = [round(height * j / ny) for j in range(ny + 1)]

    def source(x: float, y: float) -> tuple[float, float]:
        steps = 24
        rise = sum(math.tan(math.radians(angle(centre + (x - centre) * (k + 0.5) / steps, y))) for k in range(steps))
        return (x, y + rise * (x - centre) / steps)

    data = []
    for j in range(ny):
        for i in range(nx):
            nw, sw = source(xs[i], ys[j]), source(xs[i], ys[j + 1])
            se, ne = source(xs[i + 1], ys[j + 1]), source(xs[i + 1], ys[j])
            data.append(((xs[i], ys[j], xs[i + 1], ys[j + 1]), (*nw, *sw, *se, *ne)))
    out = g.transform((width, height), Image.Transform.MESH, data, resample=Image.Resampling.BICUBIC, fillcolor=255)
    return out, spread


def rotate(image: Image.Image, angle: float) -> Image.Image:
    """Rotated counter-clockwise by ``angle`` degrees, enlarged to keep every corner, on white."""
    if abs(angle) < 0.05:
        return image
    fill: int | tuple[int, int, int] = 255 if image.mode == "L" else (255, 255, 255)
    return image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=fill)


#: How much to turn an image (counter-clockwise, in degrees) to put a QR code of each zbar orientation upright.
QUARTER_TURNS = {"UP": 0, "RIGHT": 90, "DOWN": 180, "LEFT": 270}


def orientation_by_tesseract(image: Image.Image, work: Path) -> int:
    """The counter-clockwise turn (0, 90, 180 or 270) tesseract's orientation detection says puts the text upright; 0 if unsure."""
    path = work / "osd.png"
    scaled(grey(image), min(1.0, 1600 / max(1, image.width))).save(path)
    try:
        run = subprocess.run([tool("tesseract"), str(path), "stdout", "--psm", "0"], capture_output=True, text=True, timeout=TOOL_TIMEOUT, check=False, env=tesseract_env())
    except subprocess.TimeoutExpired:
        return 0
    match = re.search(r"Rotate:\s*(\d+)", run.stdout)
    confidence = re.search(r"Orientation confidence:\s*([\d.]+)", run.stdout)
    if match is None or confidence is None or float(confidence.group(1)) < 2:
        return 0
    # tesseract reports the clockwise rotation that makes the text upright.
    return (360 - int(match.group(1))) % 360


@dataclass(frozen=True)
class Prepared:
    """The page made upright and level for reading, and what was done to it."""

    image: Image.Image
    quarter_turn: int
    skew: float
    scale: float
    #: How much the line angle varied across the page and was straightened (`rectify`), in degrees; 0 if it was not.
    rectified: float = 0.0
    #: The period, in pixels, of the screen's moiré that was suppressed (`descreen`), or None.
    moire: float | None = None


def prepare(image: Image.Image, work: Path, qr_orientation: str | None = None, *, columns: int = 1) -> Prepared:
    """The page upright, level and straightened, enlarged if needed to `READ_WIDTH` for its shape (see the module documentation)."""
    turn = QUARTER_TURNS.get(qr_orientation or "", None)
    if turn is None:
        turn = orientation_by_tesseract(image, work)
    upright = image.rotate(turn, expand=True) if turn else image
    period = moire_period(upright)
    if period is not None:
        upright = descreen(upright, period)
    angle = skew_angle(upright)
    level = rotate(upright, angle)
    level, spread = rectify(level, columns)
    width = READ_WIDTH["landscape" if level.width > level.height else "portrait"]
    factor = max(1.0, width / max(1, level.width))
    return Prepared(scaled(grey(level), factor), turn, angle, factor, spread, period)


# ---------------------------------------------------------------------------
# QR codes


@dataclass(frozen=True)
class QrCode:
    data: str
    orientation: str | None
    #: The preparation it was read from.
    preparation: str


_ZBAR_NS = "{http://zbar.sourceforge.net/2008/barcode}"


def _zbar(path: Path) -> list[tuple[str, str | None]]:
    try:
        run = subprocess.run(
            [tool("zbarimg"), "--xml", "-q", "-Sdisable", "-Sqrcode.enable", str(path)],
            capture_output=True, timeout=TOOL_TIMEOUT, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return []
    if not run.stdout.strip():
        return []
    try:
        root = ET.fromstring(run.stdout)
    except ET.ParseError:
        return []
    found = []
    for symbol in root.iter(f"{_ZBAR_NS}symbol"):
        data = symbol.find(f"{_ZBAR_NS}data")
        if data is not None and data.text:
            found.append((data.text.strip(), symbol.get("orientation")))
    return found


def _corner(image: Image.Image, corner: int) -> Image.Image:
    """One corner of the image (0 top right, 1 top left, 2 bottom left, 3 bottom right), 45 percent of each side."""
    w, h = image.size
    cw, ch = int(w * 0.45), int(h * 0.45)
    x = (w - cw, 0, 0, w - cw)[corner]
    y = (0, 0, h - ch, h - ch)[corner]
    return image.crop((x, y, x + cw, y + ch))


def qr_preparations(image: Image.Image) -> list[tuple[str, Callable[[], Image.Image]]]:
    """The preparations of an image tried in turn for its QR code.

    The page's code is in a corner (22 mm; 10 to 12 mm on pages printed
    before 26 October 2026), and a photo from afar gives it few pixels per
    module: after the whole image, its corners (where the code is,
    whichever way up the page is) are tried enlarged.
    """
    longest = max(image.width, image.height)
    steps: list[tuple[str, Callable[[], Image.Image]]] = [
        ("as is", lambda: image),
        ("grey, stretched", lambda: stretch(image)),
        ("flattened", lambda: flatten(image)),
        ("black and white", lambda: binarize(image)),
    ]
    if longest < 3000:
        steps.append(("enlarged", lambda: scaled(stretch(image), 2)))
    if longest > 1600:
        steps.append(("reduced", lambda: scaled(stretch(image), 1600 / longest)))
    factor = max(1.0, min(4.0, 2700 / (0.45 * longest)))

    def corner(c: int, threshold: bool) -> Image.Image:
        enlarged = scaled(stretch(_corner(image, c)), factor)
        return enlarged.point(lambda v: 255 if v > 128 else 0) if threshold else enlarged.filter(ImageFilter.UnsharpMask(3, 250, 2))

    for c, name in enumerate(("top right", "top left", "bottom left", "bottom right")):
        steps.append((f"{name} corner, enlarged, black and white", lambda c=c: corner(c, True)))  # type: ignore[misc]
        steps.append((f"{name} corner, enlarged, sharpened", lambda c=c: corner(c, False)))  # type: ignore[misc]
    steps.append(("blurred slightly", lambda: stretch(image).filter(ImageFilter.GaussianBlur(1))))
    return steps


#: The sides of the windows `qr_windows` looks in, as fractions of the image's longest side.
QR_WINDOW_SIZES = (0.015, 0.025, 0.04, 0.065, 0.1)
#: How many more windows are looked at, once one has read, for a second page's QR code in the same photo.
SECOND_CODE_WINDOWS = 6


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / smaller if smaller else 0.0


def qr_windows(image: Image.Image, keep: int = 4) -> list[tuple[int, int, int, int]]:
    """Square regions likely to hold a small QR code, likeliest first.

    A QR code is about half dark, and changes from dark to light often both
    across and down; text is much less dark (under a fifth), and a solid
    dark area (a laptop's bezel) changes rarely. Windows of a few sizes
    (`QR_WINDOW_SIZES`) are scored on a reduced copy, and the best few of
    each size kept, without near-duplicates.
    """
    g = stretch(image)
    factor = min(1.0, 1200 / max(1, max(g.size)))
    small = scaled(g, factor)
    dark = small.point(lambda v: 255 if v < 110 else 0)
    across = small.filter(ImageFilter.Kernel((3, 3), (0, 0, 0, -1, 0, 1, 0, 0, 0), 1)).point(lambda v: 255 if v > 40 else 0)
    down = small.filter(ImageFilter.Kernel((3, 3), (0, -1, 0, 0, 0, 0, 0, 1, 0), 1)).point(lambda v: 255 if v > 40 else 0)
    scored: list[tuple[float, tuple[int, int, int, int]]] = []
    for fraction in QR_WINDOW_SIZES:
        step = max(2, round(fraction * max(small.size)) // 3)
        cw, ch = small.width // step, small.height // step
        if cw < 3 or ch < 3:
            continue

        def cells(im: Image.Image, cw: int = cw, ch: int = ch, step: int = step) -> list[int]:
            return pixels(im.crop((0, 0, cw * step, ch * step)).resize((cw, ch), Image.Resampling.BOX))

        dk, ac, dn = cells(dark), cells(across), cells(down)
        for y in range(ch - 2):
            for x in range(cw - 2):
                idx = [(y + a) * cw + x + b for a in range(3) for b in range(3)]
                density = sum(dk[i] for i in idx) / (255 * 9)
                if not 0.3 <= density <= 0.8:
                    continue
                score = min(sum(ac[i] for i in idx), sum(dn[i] for i in idx)) / (255 * 9)
                if score >= 0.08:
                    box = (round(x * step / factor), round(y * step / factor), round((x + 3) * step / factor), round((y + 3) * step / factor))
                    scored.append((score, box))
    scored.sort(key=lambda item: -item[0])
    chosen: list[tuple[int, int, int, int]] = []
    for _, box in scored:
        if any(_overlap(box, other) > 0.4 for other in chosen):
            continue
        chosen.append(box)
        if len(chosen) >= keep * len(QR_WINDOW_SIZES):
            break
    return chosen


def _window_crops(g: Image.Image, box: tuple[int, int, int, int]) -> Iterator[tuple[str, Image.Image]]:
    """A window of a grey image, with some margin, contrast stretched and enlarged (Lanczos: sharper than bicubic, which zbar needs here) to about 600 pixels, a few ways."""
    side = box[2] - box[0]
    for pad, blur in ((side // 2, False), (side, False), (side // 2, True)):
        crop = g.crop((max(0, box[0] - pad), max(0, box[1] - pad), min(g.width, box[2] + pad), min(g.height, box[3] + pad)))
        if blur:
            crop = crop.filter(ImageFilter.GaussianBlur(0.8))
        crop = ImageOps.autocontrast(crop, cutoff=2)
        factor = max(1.0, min(8.0, 600 / max(1, max(crop.size))))
        enlarged = crop.resize((round(crop.width * factor), round(crop.height * factor)), Image.Resampling.LANCZOS)
        yield f"window at {box[0]},{box[1]}", ImageOps.expand(enlarged, border=30, fill=255)


def _window_codes(image: Image.Image, work: Path) -> list[QrCode]:
    """The QR codes read from `qr_windows`: once one window reads, the windows elsewhere are still tried, so that a second page's code is not missed."""
    found: list[QrCode] = []
    read_at: list[tuple[int, int, int, int]] = []
    g = grey(image)
    after_hit = 0
    for box in qr_windows(image):
        if any(_overlap(box, other) > 0 for other in read_at):
            continue
        if read_at:
            # Once one window has read, a few of the next likeliest are looked at quickly, for a second page's code.
            after_hit += 1
            if after_hit > SECOND_CODE_WINDOWS:
                break
        for n, (name, crop) in enumerate(_window_crops(g, box)):
            if read_at and n > 0:
                break
            path = work / "qr-window.png"
            crop.save(path)
            codes = _zbar(path)
            path.unlink(missing_ok=True)
            if codes:
                found += [QrCode(data, orientation, name) for data, orientation in codes]
                read_at.append(box)
                break
    unique: dict[str, QrCode] = {}
    for code in found:
        unique.setdefault(code.data, code)
    return list(unique.values())


def decode_qr(image: Image.Image, work: Path) -> list[QrCode]:
    """Every QR code zbar reads: first from the likeliest small windows (`qr_windows`), which is quick and reads codes too small for the whole image; then from the first preparation of ``image`` that yields any (see `qr_preparations`)."""
    windowed = _window_codes(image, work)
    if windowed:
        return windowed
    for n, (name, make) in enumerate(qr_preparations(image)):
        path = work / f"qr-{n}.png"
        make().save(path)
        found = _zbar(path)
        path.unlink(missing_ok=True)
        if found:
            unique = list(dict.fromkeys(found))
            return [QrCode(data, orientation, name) for data, orientation in unique]
    return []


# ---------------------------------------------------------------------------
# Text


@dataclass(frozen=True)
class Word:
    text: str
    conf: float
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height


@dataclass
class Line:
    """One line of text as tesseract grouped it, with its words."""

    words: list[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def conf(self) -> float:
        return sum(w.conf for w in self.words) / len(self.words) if self.words else 0.0

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (
            min(w.left for w in self.words),
            min(w.top for w in self.words),
            max(w.right for w in self.words),
            max(w.bottom for w in self.words),
        )


def tesseract_env() -> dict[str, str]:
    """The environment tesseract runs in: single-threaded (``OMP_THREAD_LIMIT=1``) unless the environment says otherwise, since checks run side by side would otherwise oversubscribe the processors."""
    return dict(os.environ, OMP_THREAD_LIMIT=os.environ.get("OMP_THREAD_LIMIT", "1"))


def _tesseract_tsv(path: Path, psm: int, extra: tuple[str, ...] = ()) -> str:
    env = tesseract_env()
    try:
        run = subprocess.run(
            [tool("tesseract"), str(path), "stdout", "--psm", str(psm), "--dpi", "300", "-c", "preserve_interword_spaces=0", *extra, "tsv"],
            capture_output=True, text=True, timeout=TOOL_TIMEOUT, check=False, env=env,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return ""
    return run.stdout


def parse_tsv(text: str, dx: int = 0, dy: int = 0) -> list[Line]:
    """tesseract's TSV output as lines of words (offset by ``dx``, ``dy``), in its reading order."""
    lines: dict[tuple[int, int, int, int], Line] = {}
    for row in text.splitlines()[1:]:
        parts = row.split("\t")
        if len(parts) < 12 or parts[0] != "5":
            continue
        word = parts[11].strip()
        if not word:
            continue
        try:
            key = (int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]))
            left, top, width, height = (int(p) for p in parts[6:10])
            conf = float(parts[10])
        except ValueError:
            continue
        lines.setdefault(key, Line()).words.append(Word(word, conf, left + dx, top + dy, width, height))
    return [line for line in lines.values() if line.words]


def ocr(image: Image.Image, work: Path, *, psm: int = 6, name: str = "page") -> list[Line]:
    """tesseract's lines of ``image`` (page segmentation mode ``psm``)."""
    path = work / f"{name}.png"
    image.save(path)
    try:
        return parse_tsv(_tesseract_tsv(path, psm))
    finally:
        path.unlink(missing_ok=True)


def ocr_region(
    image: Image.Image, box: tuple[int, int, int, int], work: Path, *, whitelist: str | None = None, factor: float = 2.5, tight: bool = False
) -> Line:
    """One region of ``image`` read again as a single line, scaled by ``factor`` (0: to about 100 pixels high), optionally limited to the characters in ``whitelist``.

    The region is widened by half its height on each side, unless ``tight``
    (a region already cut clear of its neighbours), and only slightly above
    and below.
    """
    left, top, right, bottom = box
    # Barely any margin above and below: the lines are close, and a sliver of the next one spoils the reading.
    pad = max(2, (bottom - top) // 10)
    side = 0 if tight else max(4, (bottom - top) // 2)
    crop = image.crop((max(0, left - side), max(0, top - pad), min(image.width, right + side), min(image.height, bottom + pad)))
    if factor <= 0:  # the line about 100 pixels high
        factor = min(4.0, max(1.0, 100 / max(1, bottom - top)))
    crop = scaled(stretch(crop), factor)
    crop = ImageOps.expand(crop, border=20, fill=255)
    path = work / "region.png"
    crop.save(path)
    extra = ("-c", f"tessedit_char_whitelist={whitelist}") if whitelist else ()
    try:
        lines = parse_tsv(_tesseract_tsv(path, 7, extra))
    finally:
        path.unlink(missing_ok=True)
    words = [w for line in lines for w in line.words]
    return Line(words)


def text_extent(image: Image.Image, box: tuple[int, int, int, int], reach: int = 0) -> tuple[int, int, int, int] | None:
    """The first run of text in a region, left to right: from its first inked column to the first gap wider than about a third of the region's height.

    ``reach`` extends the search that far right of the region. Used to cut
    an imprinted ID out of its line clear of the rule that follows it.
    """
    left, top, right, bottom = box
    right = min(image.width, right + reach)
    if right - left < 2 or bottom - top < 2:
        return None
    region = grey(image).crop((left, top, right, bottom)).point(lambda v: 0 if v < 140 else 255)
    columns = pixels(region.resize((region.width, 1), Image.Resampling.BOX))
    inked = [int(c) < 250 for c in columns]
    if True not in inked:
        return None
    start = inked.index(True)
    gap = max(5, (bottom - top) // 3)
    end = start
    blank = 0
    for x in range(start, len(inked)):
        if inked[x]:
            end, blank = x, 0
        else:
            blank += 1
            if blank >= gap:
                break
    return (left + max(0, start - 2), top, left + end + 3, bottom)


def column_gaps(image: Image.Image, top: int = 0, columns: int = 2) -> list[int] | None:
    """The x of each white gap between a page's ``columns`` columns, each searched within a third of a column of where it should be, below ``top``; None if any is missing."""
    if columns < 2:
        return []
    body = grey(image).crop((0, min(top, image.height - 1), image.width, image.height))
    small = scaled(body, min(1.0, 1000 / max(1, body.width)))
    factor = body.width / small.width
    profile = pixels(small.resize((small.width, 1), Image.Resampling.BOX))
    n = len(profile)
    window = 3
    gaps = []
    for k in range(1, columns):
        lo, hi = round(n * (k - 1 / 3) / columns), round(n * (k + 1 / 3) / columns)
        if hi - window <= lo:
            return None
        best = max(range(lo, hi - window), key=lambda x: sum(int(c) for c in profile[x : x + window]))
        if min(int(c) for c in profile[best : best + window]) < 235:
            return None
        gaps.append(round((best + window / 2) * factor))
    return gaps


def column_gap(image: Image.Image, top: int = 0) -> int | None:
    """The x of the white gap between a two-column page's columns (`column_gaps`), or None."""
    gaps = column_gaps(image, top, 2)
    return gaps[0] if gaps else None


def ink(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    """The share of dark pixels in a region of a grey image (0 to 1)."""
    left, top, right, bottom = box
    if right - left < 2 or bottom - top < 2:
        return 0.0
    region = grey(image).crop((left, top, right, bottom)).point(lambda v: 255 if v < 128 else 0)
    histogram = region.histogram()
    total = sum(histogram)
    return histogram[255] / total if total else 0.0


def box_interior_ink(image: Image.Image, region: tuple[int, int, int, int]) -> float | None:
    """The share of dark pixels inside the printed check box found in ``region`` (0 for an empty box), or None if no box is found there.

    The box is found by its outline: the leftmost and rightmost columns, and
    the top and bottom rows, that are mostly dark. A pen mark inside it, or
    crossing it, puts ink where an empty box has none.
    """
    left, top, right, bottom = region
    if right - left < 6 or bottom - top < 6:
        return None
    crop = grey(image).crop((left, top, right, bottom)).point(lambda v: 1 if v < 128 else 0)
    w, h = crop.size
    dark = pixels(crop)
    cols = [sum(dark[y * w + x] for y in range(h)) for x in range(w)]
    edges = [x for x, c in enumerate(cols) if c >= 0.4 * h]
    if len(edges) < 2 or edges[-1] - edges[0] < 6:
        return None
    x0, x1 = edges[0], edges[-1]
    rows = [sum(dark[y * w + x] for x in range(x0, x1 + 1)) for y in range(h)]
    lines = [y for y, c in enumerate(rows) if c >= 0.6 * (x1 - x0 + 1)]
    if len(lines) < 2 or lines[-1] - lines[0] < 4:
        return None
    y0, y1 = lines[0], lines[-1]
    inside = [dark[y * w + x] for y in range(y0 + 2, y1 - 1) for x in range(x0 + 2, x1 - 1)]
    return sum(inside) / len(inside) if inside else None
