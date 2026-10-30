"""The printed extract as a PDF: crisp, the same on every printer and phone, and byte for byte reproducible.

`render_pdf` draws one board's printout in one layout
(`corla_observer.printlayout`): its provenance sheet first, then one sheet
per page (`corla_observer.extract.board_pages`), exactly as docs/EXTRACT.md
defines them. It is drawn with ReportLab, in Python, without a browser.

**What is drawn.** Each page's sheet has a header band across its top: the
page's QR code (``COX2:`` and the page's hash, error correction M, a
version 4 symbol drawn as vector squares, `QR_SIDE` square) at the right,
as tall as the band, and the page's identification line
(`corla_observer.extract.page_line`) beside it, in DejaVu Sans Mono, its
short hashes larger, bold and letter-spaced for OCR (`IDENT_HASH_SIZE`), on
two lines if it is too long for one. Then the ballots, down each column in
turn: per ballot its imprinted ID's line (its place on the board, then
"ImprintedId ..." in bold centred between rules, then its location), its
marks line (POSSIBLE, BOUNDARY) if it has any, and one line per contest in
three cells, the contest's name right-aligned, a check box and the
choice in bold, a contest's further choices (a multi-seat contest, an
overvote) on continuation lines below, one each, under the choices; every
second contest shaded, to guide the eye. The
names are those of `corla_observer.printnames` for the layout, shortened
by measuring them in the fonts they are drawn in, so nothing wraps or
overflows. The provenance sheet (`corla_observer.extract.provenance`) has
the notices and its identification beside the extract's QR code
(``COX2E:``), then the seed and every hash in full, the key to the
shortened names and how to check a page; if it is too long for one sheet
it continues on another, which is not counted among the pages either.

**Reproducible.** The same extract, board, layout and software string
always give the same bytes: ReportLab's invariant mode (no timestamps, no
random names), no creation or modification date in the document's
information, a document ID derived from the extract's and the page index's
hashes, the layout, the board and the software string (`document_id`), the
bundled fonts subset in the order they are drawn, and nothing else from
the machine. The bytes depend on the ReportLab version (pinned by
``uv.lock``) and the bundled fonts, so `tests/test_pdfextract.py` pins the
hash of a small extract's PDF.

`pdf_sheets` gives the drawing as data (`Sheet`, with its `Text`, `Box`,
`Rule`, `Qr` and `Stroke` items) before it is drawn by `draw_pdf`, so that
tests can alter a printout as a forger or a pen would.
"""

from __future__ import annotations

import hashlib
import io
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from corla_observer import printlayout
from corla_observer.events import EventLog
from corla_observer.extract import (
    CHECK_STEPS,
    INTERIM_ORDERS,
    KEEP_OUT_OF_SIGHT,
    ORDER_NOTES,
    SEPARATOR,
    Page,
    Provenance,
    default_layout,
    extract_pdfs,
    extract_software,
    grouped,
    is_hash,
    page_line,
    print_marks,
    print_names,
    page_format,
    print_lines,
    provenance,
    provenance_rows,
    qr_payload,
    short,
)
from corla_observer.printlayout import BOLD, MARGIN, MONO, MONO_BOLD, SANS, Layout, text_width
from corla_observer.printnames import PrintNames, cut
from corla_observer.workspace import Workspace

#: The identification line's size, in DejaVu Sans Mono.
IDENT_SIZE = 9.0
#: Its short hashes ("extract acbe1a4f", "page a631fade"), which OCR must read character by character: larger, in DejaVu Sans Mono Bold (a dotted zero), and a little letter-spaced.
IDENT_HASH_SIZE = 10.0
IDENT_HASH_SPACE = 0.8
_IDENT_HASH = re.compile(r"\b(?:extract|page) ([0-9a-f]{8})\b")
#: The provenance sheet's text sizes.
PROV_SIZE = 8.0
PROV_HASH_SIZE = 7.5
PROV_LIST_SIZE = 7.5
PROV_HEADING_SIZE = 9.0
#: The shade of every second contest line, and the grey of a ballot's place and location.
SHADE = 0.94
MUTED = 0.2

_render_lock = threading.Lock()


@dataclass
class Text:
    """A run of text on one line; ``x`` is its left edge, right edge or centre as ``align`` says, ``y`` its baseline."""

    x: float
    y: float
    text: str
    font: str
    size: float
    align: str = "left"
    gray: float = 0.0
    #: What it is: "ident", "id", "place", "location", "marks", "contest", "vote", or a provenance part.
    role: str = ""
    #: The width it must fit in, if any (checked by tests: nothing overflows).
    room: float | None = None
    #: Points added after each character (letter-spacing).
    char_space: float = 0.0

    @property
    def width(self) -> float:
        return text_width(self.text, self.font, self.size, self.char_space)

    @property
    def left(self) -> float:
        return {"left": self.x, "right": self.x - self.width, "centre": self.x - self.width / 2}[self.align]


@dataclass
class Box:
    """A rectangle (``x``, ``y`` its lower left corner): a check box (stroked) or a line's shading (filled)."""

    x: float
    y: float
    w: float
    h: float
    stroke: float = 0.0
    fill: float | None = None
    role: str = ""


@dataclass
class Rule:
    x0: float
    y0: float
    x1: float
    y1: float
    width: float = 0.6


@dataclass
class Qr:
    """A QR code holding ``payload``, ``side`` square, its lower left corner at ``x``, ``y``."""

    payload: str
    x: float
    y: float
    side: float


@dataclass
class Stroke:
    """A pen stroke through ``points`` (only for tests: a hand-made check mark)."""

    points: list[tuple[float, float]]
    width: float
    rgb: tuple[float, float, float] = (0.07, 0.13, 0.2)


Item = Text | Box | Rule | Qr | Stroke


@dataclass
class Sheet:
    """One printed sheet: the provenance (``page`` None) or a page."""

    width: float
    height: float
    page: int | None
    items: list[Item] = field(default_factory=list)

    def texts(self, role: str) -> list[Text]:
        return [item for item in self.items if isinstance(item, Text) and item.role == role]


# ---------------------------------------------------------------------------
# Laying out


def wrap(runs: Sequence[tuple[str, str]], room: float, size: float, indent: float = 0.0) -> list[list[tuple[float, str, str]]]:
    """Text in runs of ``(text, font)``, word-wrapped to ``room``: per line, ``(x offset, text, font)`` pieces; lines after the first indented by ``indent``.

    For prose on the provenance sheet (notices, the key, the check steps),
    not for names, which never wrap.
    """
    words: list[tuple[str, str, bool]] = []  # (word, font, space before)
    for text, font in runs:
        for i, word in enumerate(text.split(" ")):
            if word:
                words.append((word, font, i > 0 or (text.startswith(" ") and bool(words))))
    lines: list[list[tuple[float, str, str]]] = [[]]
    x = 0.0
    space = text_width(" ", SANS, size)
    for word, font, spaced in words:
        w = text_width(word, font, size)
        gap = space if spaced and lines[-1] else 0.0
        limit = room - (indent if len(lines) > 1 else 0.0)
        if lines[-1] and x + gap + w > limit:
            lines.append([])
            x, gap = 0.0, 0.0
        if w > limit:
            word = cut(word, limit, printlayout.measure(font, size))
            w = text_width(word, font, size)
        lines[-1].append((x + gap + (indent if len(lines) > 1 else 0.0), word, font))
        x += gap + w
    return lines


def _qr_header(sheet: Sheet, lay: Layout, payload: str) -> float:
    """Draw the QR code at the top right; the x where the text beside it must end."""
    side = lay.qr_side
    sheet.items.append(Qr(payload, sheet.width - MARGIN - side, sheet.height - MARGIN - side, side))
    return sheet.width - MARGIN - side - lay.qr_quiet


def ident_runs(text: str) -> list[tuple[str, str, float, float]]:
    """An identification line as runs of ``(text, font, size, letter-spacing)``: its short hashes larger and bold (`IDENT_HASH_SIZE`)."""
    runs: list[tuple[str, str, float, float]] = []
    at = 0
    for m in _IDENT_HASH.finditer(text):
        if m.start(1) > at:
            runs.append((text[at : m.start(1)], MONO, IDENT_SIZE, 0.0))
        runs.append((m.group(1), MONO_BOLD, IDENT_HASH_SIZE, IDENT_HASH_SPACE))
        at = m.end(1)
    if at < len(text):
        runs.append((text[at:], MONO, IDENT_SIZE, 0.0))
    return runs


def _runs_width(text: str) -> float:
    return sum(text_width(t, f, s, c) for t, f, s, c in ident_runs(text))


def _ident(sheet: Sheet, text: str, right: float, top: float) -> float:
    """The identification line at the top left, on two lines if it does not fit beside the QR code; the y below it."""
    room = right - MARGIN
    lines: list[str] = []
    for part in text.split(SEPARATOR):
        if lines and _runs_width(lines[-1] + SEPARATOR + part) <= room:
            lines[-1] += SEPARATOR + part
        else:
            lines.append(part)
    y = top
    for line in lines:
        y -= IDENT_HASH_SIZE * 1.2
        x = MARGIN
        for run, font, size, space in ident_runs(line):
            sheet.items.append(Text(x, y + IDENT_SIZE * 0.25, run, font, size, role="ident", room=right - x, char_space=space))
            x += text_width(run, font, size, space)
    return y


def _ballot(
    sheet: Sheet, lay: Layout, x0: float, top: float, ballot: Mapping[str, Any], names: PrintNames, total: int, continuation: bool = True
) -> float:
    """Draw a ballot's block in the column at ``x0`` from ``top``; the y below it."""
    w = lay.column_width
    size, lead = lay.size, lay.leading
    y = top
    base = y - lay.baseline
    mid = base + lay.x_height / 2
    # The imprinted ID's line: place, rule, ID, rule, location.
    ident = f"ImprintedId {ballot['imprinted_id']}"
    id_room = w - 2 * text_width("0" * 9, SANS, size)
    ident = cut(ident, id_room, lambda t: text_width(t, BOLD, size))
    id_w = text_width(ident, BOLD, size)
    centre = x0 + w / 2
    place = f"{ballot['board_position']}/{total}"
    place_w = text_width(place, SANS, size)
    location = ballot.get("location")
    loc = cut(f"loc {location}", 0.3 * w, lambda t: text_width(t, SANS, size)) if location else ""
    loc_w = text_width(loc, SANS, size) if loc else 0.0
    gap = 1.5 * printlayout.MM
    sheet.items.append(Text(x0, base, place, SANS, size, gray=MUTED, role="place"))
    sheet.items.append(Text(centre, base, ident, BOLD, size, align="centre", role="id", room=id_room))
    if loc:
        sheet.items.append(Text(x0 + w, base, loc, SANS, size, align="right", gray=MUTED, role="location", room=0.3 * w))
    left_end, right_start = centre - id_w / 2 - gap, centre + id_w / 2 + gap
    left_start = x0 + place_w + gap
    right_end = x0 + w - (loc_w + gap if loc else 0.0)
    if left_end > left_start:
        sheet.items.append(Rule(left_start, mid, left_end, mid, 0.6))
    if right_end > right_start:
        sheet.items.append(Rule(right_start, mid, right_end, mid, 0.6))
    y -= lead
    marks = print_marks(ballot, names)
    if marks:
        sheet.items.append(Text(x0 + lay.pad, y - lay.baseline, marks, BOLD, size, role="marks", room=lay.marks_budget))
        y -= lead
    first = 2 if marks else 1  # the first contest's place in the block, the ID's line being 0: odd contests are shaded
    for line in print_lines(ballot, names, continuation):
        if (first + line.row) % 2 == 0:  # a contest's continuation lines shaded like its first
            sheet.items.append(Box(x0, y - lead, w, lead, fill=SHADE, role="shade"))
        base = y - lay.baseline
        if not line.continuation:
            sheet.items.append(Text(x0 + lay.contest_box - lay.pad, base, line.contest, SANS, size, align="right", role="contest", room=lay.contest_budget))
            bw, bh = 1.3 * size, 0.75 * size
            bx = x0 + lay.contest_box + (lay.mark_width - bw) / 2
            by = base + lay.x_height / 2 - bh / 2
            sheet.items.append(Box(bx, by, bw, bh, stroke=0.6, fill=1.0, role="box"))
        sheet.items.append(Text(x0 + lay.contest_box + lay.mark_width + lay.pad, base, line.value, BOLD, size, role="vote", room=lay.vote_budget))
        y -= lead
    return y


def page_sheet(extract: Mapping[str, Any], sha256: str, page: Page, lay: Layout, names: PrintNames, total: int) -> Sheet:
    """A page's sheet: its header band (identification line and QR code), then its ballots in columns."""
    width, height = lay.pagesize
    sheet = Sheet(width, height, page.number)
    right = _qr_header(sheet, lay, qr_payload(page))
    _ident(sheet, page_line(extract, sha256, page), right, height - MARGIN)
    top = lay.body_top
    sheet.items.append(Rule(MARGIN, top + printlayout.BODY_GAP, width - MARGIN, top + printlayout.BODY_GAP, 0.75))
    columns = page.columns or (page.ballots,)
    for c, column in enumerate(columns):
        y = top
        for ballot in column:
            y = _ballot(sheet, lay, lay.column_x(c), y, ballot, names, total, page_format(extract) >= 6)
    return sheet


class _Flow:
    """The provenance sheet's text, flowing onto a continuation sheet when it reaches the bottom margin."""

    def __init__(self, lay: Layout, prov: Provenance, head: str, notices: Sequence[tuple[str, bool]]) -> None:
        self.lay = lay
        self.prov = prov
        self.head = head
        self.sheets: list[Sheet] = []
        self.y = 0.0
        self._new_sheet(notices)

    @property
    def sheet(self) -> Sheet:
        return self.sheets[-1]

    def _new_sheet(self, notices: Sequence[tuple[str, bool]] = ()) -> None:
        width, height = self.lay.pagesize
        sheet = Sheet(width, height, None)
        self.sheets.append(sheet)
        right = _qr_header(sheet, self.lay, self.prov.qr)
        y = height - MARGIN
        for text, boxed in notices:
            lines = wrap([(text, BOLD)], right - MARGIN - (4 if boxed else 0), PROV_SIZE)
            start = y
            for line in lines:
                y -= PROV_SIZE * 1.25
                for dx, word, font in line:
                    sheet.items.append(Text(MARGIN + (2 if boxed else 0) + dx, y + PROV_SIZE * 0.3, word, font, PROV_SIZE, role="notice"))
            if boxed:
                sheet.items.append(Box(MARGIN, y - 1, right - MARGIN, start - y + 1, stroke=0.75, role="notice-box"))
                y -= 2
            y -= 2
        head = self.head + ("" if len(self.sheets) == 1 else " (continued)")
        y = _ident(sheet, head, right, y)
        bottom = min(height - MARGIN - self.lay.header_height, y - 3)
        sheet.items.append(Rule(MARGIN, bottom, width - MARGIN, bottom, 0.75))
        self.y = bottom - 4

    def need(self, height: float) -> None:
        if self.y - height < MARGIN:
            self._new_sheet()

    @property
    def width(self) -> float:
        return self.sheet.width - 2 * MARGIN


def provenance_sheets(extract: Mapping[str, Any], sha256: str, prov: Provenance, lay: Layout) -> list[Sheet]:
    """The provenance sheet(s): the notices and identification beside the extract's QR code, the facts, the key, the contests printed shorter, and how to check a page."""
    notices = [(KEEP_OUT_OF_SIGHT, True)]
    if extract["order"] in INTERIM_ORDERS:
        notices.append((ORDER_NOTES[extract["order"]], False))
    head = SEPARATOR.join([prov.county, f"Board {prov.board}", "PROVENANCE", f"extract {short(sha256)}"])
    flow = _Flow(lay, prov, head, notices)
    rows = provenance_rows(prov)
    label_w = max(text_width(label, SANS, PROV_SIZE) for label, _ in rows) + 3 * printlayout.MM
    lead = PROV_SIZE * 1.3
    for label, value in rows:
        if is_hash(value):
            text, font, size = grouped(value), MONO, PROV_HASH_SIZE
        else:
            text, font, size = value, SANS, PROV_SIZE
        lines = wrap([(text, font)], flow.width - label_w, size)
        flow.need(lead * len(lines))
        flow.sheet.items.append(Text(MARGIN, flow.y - PROV_SIZE, label, SANS, PROV_SIZE, role="fact-label"))
        for line in lines:
            for dx, word, f in line:
                flow.sheet.items.append(Text(MARGIN + label_w + dx, flow.y - PROV_SIZE, word, f, size, role="fact"))
            flow.y -= lead

    def listing(title: str, entries: Sequence[tuple[str, str]], columns: int) -> None:
        if not entries:
            return
        flow.need(PROV_HEADING_SIZE * 1.6 + PROV_LIST_SIZE * 1.25)
        flow.y -= PROV_HEADING_SIZE * 0.6
        flow.sheet.items.append(Text(MARGIN, flow.y - PROV_HEADING_SIZE, title, BOLD, PROV_HEADING_SIZE, role="heading"))
        flow.y -= PROV_HEADING_SIZE * 1.4
        gap = 6 * printlayout.MM
        col_w = (flow.width - (columns - 1) * gap) / columns
        lead = PROV_LIST_SIZE * 1.25
        blocks = [wrap([(a, BOLD), (" = " + b, SANS)], col_w, PROV_LIST_SIZE, indent=2 * printlayout.MM) for a, b in entries]
        fresh = False
        while blocks:
            # Balance the remaining entries across the columns, as far as the sheet has room.
            total = sum(len(b) for b in blocks)
            room = int((flow.y - MARGIN) / lead)
            if room < len(blocks[0]) and not fresh:
                flow.need(flow.y)  # not even the first entry fits: a new sheet
                fresh = True
                continue
            per = max(len(blocks[0]), min(room, -(-total // columns)))
            fresh = False
            top = flow.y
            used = 0
            for c in range(columns):
                y = top
                count = 0
                while blocks and count + len(blocks[0]) <= per:
                    for line in blocks.pop(0):
                        y -= lead
                        count += 1
                        for dx, word, f in line:
                            flow.sheet.items.append(Text(MARGIN + c * (col_w + gap) + dx, y + PROV_LIST_SIZE * 0.25, word, f, PROV_LIST_SIZE, role="list"))
                used = max(used, count)
            flow.y = top - used * lead
            if blocks:
                flow.need(flow.y)
                fresh = True

    listing("Key", list(prov.key), 2)
    listing("Contests printed shorter", list(prov.shortened), 3 if lay.landscape else 2)
    flow.need(PROV_HEADING_SIZE * 2)
    flow.y -= PROV_HEADING_SIZE * 0.6
    flow.sheet.items.append(Text(MARGIN, flow.y - PROV_HEADING_SIZE, "To check a printed page", BOLD, PROV_HEADING_SIZE, role="heading"))
    flow.y -= PROV_HEADING_SIZE * 1.4
    for n, step in enumerate(CHECK_STEPS, start=1):
        lines = wrap([(f"{n}. {step}", SANS)], flow.width, PROV_SIZE, indent=3 * printlayout.MM)
        flow.need(lead * len(lines))
        for line in lines:
            flow.y -= lead
            for dx, word, f in line:
                flow.sheet.items.append(Text(MARGIN + dx, flow.y + PROV_SIZE * 0.25, word, f, PROV_SIZE, role="step"))
    return flow.sheets


def pdf_sheets(
    extract: Mapping[str, Any], sha256: str, board: int, layout: str | None = None, *, software: str | None = None, geometry: Layout | None = None
) -> list[Sheet]:
    """A board's printout in ``layout`` as data: its provenance sheet(s), then one sheet per page.

    ``geometry`` draws it on another sheet geometry (only to measure other QR code sizes; the pages are still ``layout``'s).
    """
    layout = layout or default_layout(extract)
    lay = geometry or printlayout.layout(layout)
    names = print_names(extract, layout)
    prov = provenance(extract, sha256, board, software=software, layout=layout, names=names)
    total = sum(1 for b in extract["ballots"] if b["board"] == board)
    sheets = provenance_sheets(extract, sha256, prov, lay)
    sheets += [page_sheet(extract, sha256, page, lay, names, total) for page in prov.pages]
    return sheets


# ---------------------------------------------------------------------------
# Drawing


def document_id(extract_sha256: str, page_index_sha256: str, layout: str, board: int, software: str | None) -> bytes:
    """The PDF's document ID: the first 16 bytes of the SHA-256 of these, one per line."""
    text = "\n".join(["corla-observer extract printout", extract_sha256, page_index_sha256, layout, str(board), software or ""])
    return hashlib.sha256(text.encode("utf-8")).digest()[:16]


def _qr_path(canvas: Any, item: Qr) -> None:
    import segno

    code = segno.make(item.payload, error="m", micro=False, boost_error=False)
    matrix = code.matrix
    m = item.side / len(matrix)
    path = canvas.beginPath()
    top = item.y + item.side
    for r, row in enumerate(matrix):
        c = 0
        while c < len(row):
            if row[c]:
                start = c
                while c < len(row) and row[c]:
                    c += 1
                path.rect(item.x + start * m, top - (r + 1) * m, (c - start) * m, m)
            else:
                c += 1
    canvas.setFillGray(0)
    canvas.drawPath(path, stroke=0, fill=1)


def draw_pdf(sheets: Iterable[Sheet], *, title: str, subject: str, doc_id: bytes) -> bytes:
    """Draw ``sheets`` as a PDF, reproducibly (see the module documentation)."""
    printlayout.register_fonts()
    from reportlab.pdfbase.pdfdoc import PDFDictionary, PDFInfo, PDFName, PDFString
    from reportlab.pdfgen.canvas import Canvas

    class Info(PDFInfo):
        """The document information, without a creation or modification date."""

        def format(self, document: Any) -> bytes:
            entries = {
                "Title": PDFString(title),
                "Subject": PDFString(subject),
                "Creator": PDFString("corla-observer"),
                "Producer": PDFString("corla-observer (ReportLab)"),
                "Trapped": PDFName("False"),
            }
            out: bytes = PDFDictionary(entries).format(document)
            return out

    buf = io.BytesIO()
    with _render_lock:
        canvas = Canvas(buf, pagesize=printlayout.LETTER, invariant=1, pageCompression=1)
        doc: Any = getattr(canvas, "_doc")  # noqa: B009 - ReportLab's document, to set its information and ID
        doc.info = Info()
        hexid = doc_id.hex().encode("ascii")
        doc._ID = b"\n[<" + hexid + b"><" + hexid + b">]\n"
        for sheet in sheets:
            canvas.setPageSize((sheet.width, sheet.height))
            for item in sorted(sheet.items, key=lambda i: 0 if isinstance(i, Box) and i.role == "shade" else 1):
                if isinstance(item, Text):
                    canvas.setFillGray(item.gray)
                    canvas.setFont(item.font, item.size)
                    canvas.drawString(item.left, item.y, item.text, charSpace=item.char_space)
                elif isinstance(item, Box):
                    if item.fill is not None:
                        canvas.setFillGray(item.fill)
                    if item.stroke:
                        canvas.setLineWidth(item.stroke)
                        canvas.setStrokeGray(0)
                    canvas.rect(item.x, item.y, item.w, item.h, stroke=1 if item.stroke else 0, fill=1 if item.fill is not None else 0)
                elif isinstance(item, Rule):
                    canvas.setLineWidth(item.width)
                    canvas.setStrokeGray(0)
                    canvas.line(item.x0, item.y0, item.x1, item.y1)
                elif isinstance(item, Qr):
                    _qr_path(canvas, item)
                elif isinstance(item, Stroke):
                    canvas.setStrokeColorRGB(*item.rgb)
                    canvas.setLineWidth(item.width)
                    canvas.setLineCap(1)
                    canvas.setLineJoin(1)
                    path = canvas.beginPath()
                    path.moveTo(*item.points[0])
                    for point in item.points[1:]:
                        path.lineTo(*point)
                    canvas.drawPath(path, stroke=1, fill=0)
                    canvas.setLineCap(0)
                    canvas.setLineJoin(0)
            canvas.showPage()
        canvas.save()
    return buf.getvalue()


def render_pdf(
    extract: Mapping[str, Any],
    sha256: str,
    board: int,
    layout: str | None = None,
    *,
    software: str | None = None,
    sheets: Sequence[Sheet] | None = None,
) -> bytes:
    """A board's printout in ``layout`` as PDF bytes: the provenance sheet, then each page (see the module documentation).

    ``software`` is the version that built the extract, as its ``extract.built`` event records it (on the provenance sheet).
    ``sheets``, if given, are drawn instead of the printout's own (`pdf_sheets`, altered).
    """
    from corla_observer.extract import page_index
    from corla_observer.events import canonical_json

    layout = layout or default_layout(extract)
    if sheets is None:
        sheets = pdf_sheets(extract, sha256, board, layout, software=software)
    index_sha = hashlib.sha256(canonical_json(page_index(extract, sha256))).hexdigest()
    return draw_pdf(
        sheets,
        title=f"Extract: {extract['county']}, audit board {board}, {layout}",
        subject=f"extract {sha256}; page index {index_sha}",
        doc_id=document_id(sha256, index_sha, layout, board, software),
    )


def printout(workspace: Workspace, log: EventLog, extract: Mapping[str, Any], sha256: str, board: int, layout: str) -> bytes:
    """A board's printout in ``layout``: the PDF recorded when the extract was built (``pdfs`` in its ``extract.built`` event), if stored, else drawn anew, which gives the same bytes."""
    recorded = extract_pdfs(log, sha256).get(layout, {}).get(str(board))
    if recorded and workspace.has(recorded):
        return workspace.get_bytes(recorded)
    return render_pdf(extract, sha256, board, layout, software=extract_software(log, sha256))


def pdf_filename(extract: Mapping[str, Any], sha256: str, board: int, layout: str) -> str:
    """The printout's file name: ``weld-board1-landscape-2-acbe1a4f.pdf``."""
    county = "".join(ch if ch.isalnum() else "-" for ch in str(extract["county"]).lower())
    return f"{county}-board{board}-{layout}-{sha256[:8]}.pdf"

