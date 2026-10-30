"""The printed extract's sheets: fonts, their metrics, and each print layout's geometry.

The printout is a PDF drawn by `corla_observer.pdfextract`. Everything about
where things go on a sheet, and so how many lines a column holds and how
wide a printed name may be, is defined here, from the bundled fonts' real
metrics, so that the pagination (`corla_observer.extract.LAYOUTS`), the
shortened names (`corla_observer.printnames`) and the drawing agree exactly:
nothing printed ever wraps or overflows its column.

**Fonts.** DejaVu Sans Condensed (regular and bold) for the text and DejaVu
Sans Mono for hashes and the identification line, version 2.37, bundled
unmodified in ``fonts/`` with their licence (``fonts/LICENSE-DejaVu``: the
Bitstream Vera and Arev licences, which allow redistribution and
embedding). A narrow, metric-stable, freely licensed sans serif: the same
file always measures and draws the same, whatever is installed on the
machine. The text sizes are chosen against the July 2026 Weld printout,
Courier at 7.46 pt: its x-height (Courier's is 0.426 em, so 3.18 pt) is the
smallest found readable, and every layout's text has a larger x-height
(DejaVu's is 0.547 em: 3.55 pt at 6.5 pt, 4.10 pt at 7.5 pt).

**Layouts** (`LAYOUTS`): a letter sheet, portrait or landscape, with one,
two or three columns of ballots. Each sheet is one page, with a header band
across its top: the page's QR code at the right, as tall as the band, and
the identification line beside it. The QR code is `QR_SIDE` square, chosen
by measurement (``scripts/qr_size_rates.py``, docs/EXTRACT.md) so that a
phone photo of the whole sheet decodes it reliably; the band is the code
and its quiet zone. Below the band, each column holds `Layout.column_lines`
lines of `Layout.leading` points each.

A ballot's block in a column: its imprinted ID's line (its place on the
board at the left, "ImprintedId ..." in bold, centred between rules, its
location at the right), its marks line if any, and one line per contest in
three cells: the contest's name right-aligned (`Layout.contest_box`), a
check box (`Layout.mark_box`), and the choices in bold (`Layout.vote_box`).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "fonts"
#: The fonts used, by role: the file in ``fonts/`` and the name it is registered under.
FONT_FILES = {
    "sans": ("DejaVuSansCondensed.ttf", "COSans"),
    "bold": ("DejaVuSansCondensed-Bold.ttf", "COSansBold"),
    "mono": ("DejaVuSansMono.ttf", "COMono"),
    "mono-bold": ("DejaVuSansMono-Bold.ttf", "COMonoBold"),
}
SANS, BOLD, MONO, MONO_BOLD = (FONT_FILES[role][1] for role in ("sans", "bold", "mono", "mono-bold"))
#: The fonts' ascent and descent (hhea, 2048 units per em) and x-height, in ems: the same for all three.
ASCENT = 1556 / 2048
DESCENT = 492 / 2048
X_HEIGHT = 1120 / 2048
#: The July 2026 Weld printout's Courier at 7.4625 pt has this x-height (Courier's x-height is 426/1000 em): the smallest found readable.
MIN_X_HEIGHT = 7.4625 * 0.426

MM = 72 / 25.4
LETTER = (612.0, 792.0)
#: The sheet's margin on every side: 1/4 inch, within what printers print.
MARGIN = 18.0

#: The space between the header band's rule and the first line of ballots.
BODY_GAP = 2.0
#: The QR code's side, including no quiet zone: 22 mm, so that a version 4 symbol (33 modules) has modules of 2/3 mm.
QR_SIDE = 22 * MM
QR_MODULES = 33
#: The white space kept around the code (the QR standard's quiet zone of 4 modules) where the margin does not give it.
QR_QUIET = 4 * QR_SIDE / QR_MODULES

_lock = threading.Lock()
_registered = False


def register_fonts() -> None:
    """Register the bundled fonts with ReportLab (once)."""
    global _registered
    with _lock:
        if _registered:
            return
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        for filename, name in FONT_FILES.values():
            pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / filename)))
        _registered = True


def text_width(text: str, font: str, size: float, char_space: float = 0.0) -> float:
    """The width of ``text`` in points, in a bundled font at ``size`` points (no kerning), with ``char_space`` points added after each character."""
    register_fonts()
    from reportlab.pdfbase.pdfmetrics import stringWidth

    return float(stringWidth(text, font, size)) + char_space * len(text)


@cache
def measure(font: str, size: float) -> Callable[[str], float]:
    """A function giving a text's width in points in ``font`` at ``size``."""
    return lambda text: text_width(text, font, size)


@dataclass(frozen=True)
class Layout:
    """A print layout: the sheet, its columns of ballots and the text's size."""

    name: str
    landscape: bool
    columns: int
    size: float
    leading: float
    gap: float
    description: str
    #: The QR code's side (`QR_SIDE`; another only to measure how well other sizes decode).
    qr_side: float = QR_SIDE

    @property
    def pagesize(self) -> tuple[float, float]:
        w, h = LETTER
        return (h, w) if self.landscape else (w, h)

    @property
    def width(self) -> float:
        return self.pagesize[0] - 2 * MARGIN

    @property
    def header_height(self) -> float:
        """The header band: the QR code and its quiet zone below it (the margin gives it above and to the right)."""
        return self.qr_side + self.qr_quiet

    @property
    def qr_quiet(self) -> float:
        """The QR code's quiet zone: 4 modules."""
        return 4 * self.qr_side / QR_MODULES

    @property
    def body_top(self) -> float:
        """The y (from the bottom of the sheet) where the first line of ballots starts: below the band's rule."""
        return self.pagesize[1] - MARGIN - self.header_height - BODY_GAP

    @property
    def column_lines(self) -> int:
        """Lines per column: as many whole lines as fit between the header band and the bottom margin."""
        return int((self.body_top - MARGIN) / self.leading + 1e-9)

    @property
    def column_width(self) -> float:
        return (self.width - (self.columns - 1) * self.gap) / self.columns

    def column_x(self, column: int) -> float:
        return MARGIN + column * (self.column_width + self.gap)

    @property
    def pad(self) -> float:
        """Space kept at each side of a cell's text."""
        return 0.15 * self.size

    @property
    def mark_width(self) -> float:
        """The check box's cell: 2 ems."""
        return 2 * self.size

    @property
    def contest_box(self) -> float:
        """The contest name's cell: half the column."""
        return self.column_width / 2

    @property
    def contest_budget(self) -> float:
        return self.contest_box - 2 * self.pad

    @property
    def vote_box(self) -> float:
        return self.column_width - self.contest_box - self.mark_width

    @property
    def vote_budget(self) -> float:
        return self.vote_box - 2 * self.pad

    @property
    def marks_budget(self) -> float:
        return self.column_width - 2 * self.pad

    @property
    def baseline(self) -> float:
        """From the top of a line to its baseline: the line's half-leading above the fonts' ascent."""
        return (self.leading - (ASCENT + DESCENT) * self.size) / 2 + ASCENT * self.size

    @property
    def x_height(self) -> float:
        return X_HEIGHT * self.size


#: The print layouts, by name, the default (`corla_observer.extract.DEFAULT_LAYOUT`) first.
#: Each sheet is one page; the ballots run down each column in turn, left to right.
LAYOUTS: dict[str, Layout] = {
    "portrait-2": Layout("portrait-2", False, 2, 6.5, 7.8, 5 * MM, "portrait, two columns"),
    "portrait-1": Layout("portrait-1", False, 1, 7.5, 9.0, 0.0, "portrait, one column"),
    "landscape-2": Layout("landscape-2", True, 2, 6.5, 7.8, 6 * MM, "landscape, two columns"),
    "landscape-3": Layout("landscape-3", True, 3, 6.5, 7.8, 5 * MM, "landscape, three columns"),
}
#: The layout a version 1 extract's pages (page format 2, portrait, 70 lines) are drawn in.
LEGACY_LAYOUT = "portrait-1"


def layout(name: str) -> Layout:
    """A layout by name; a version 1 extract's "portrait" is drawn as `LEGACY_LAYOUT`."""
    return LAYOUTS[LEGACY_LAYOUT if name == "portrait" else name]
