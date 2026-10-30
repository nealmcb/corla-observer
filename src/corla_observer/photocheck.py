"""The photo check: does a photographed or scanned printed extract page match the committed extract?

docs/PHOTO_CHECK.md describes the check for its users; this is how it is
done. `check_photo` takes an image and a workspace:

1. **Find the page.** It reads the QR code (`corla_observer.photoimage`):
   ``COX2:`` and a page's SHA-256, or, on a provenance sheet, ``COX2E:``
   and the extract's. It looks the hash up in the page index stored for
   every extract the workspace has built (`PageFinder`, both layouts, both
   purposes, superseded extracts included). Without a readable QR code it
   finds the page's identification line, wherever it is in the photo, and
   looks up the extract's and the page's short hashes instead, exactly or,
   failing that, allowing for a few misread characters where the board and
   page number read agree and no other page comes close
   (`PageFinder.by_ident`); the page is then only located, not confirmed. It then lists the recorded evidence commitments
   (``evidence.committed``) whose SUMS file lists the page index or the
   extract (`commitments`), with each one's proofs: the OpenTimestamps
   proof's Bitcoin block once upgraded, the Sigstore (Rekor) log index and
   the git commit.
2. **Read the printed text.** It puts the page upright and level, then
   reads it with tesseract twice, differently prepared (as photographed,
   and flattened against glare), each column of a page of several on its own.
   Each pass's lines are aligned with the lines the page should have, as
   printed (`expected_lines`: the identification line, each ballot's
   imprinted ID line, its marks and its contests with the printed,
   shortened names of `corla_observer.printnames`), by a sequence alignment
   that keeps their order. A line not settled by the two passes is read
   again on its own, enlarged.
3. **Compare.** Each expected line is judged on the readings it got
   (`_judge_votes`): **matched** only if more readings show exactly what
   should be printed (for a choice: closest to it among the contest's
   possible printed values, by a clear margin) than show anything else;
   **differs** only if no reading shows it and at least two readings agree
   on the same other content; otherwise **unread**. So an unreadable line
   is unread, never matched, and OCR uncertainty can turn a difference into
   an unread line but never into a match. A re-read of a line on its own
   counts one and a half times a whole-page pass. Lines that should not be
   there (another ballot's imprinted ID, an extra contest line), read alike
   by two passes, are differences too. On a page read so poorly that its
   differing contest lines are more than a quarter of its matched ones,
   with nothing firmer differing, those differences are reported as unread
   (`_distrust_scattered_rows`).

Two separate answers come out of a check. The content verdict
(`Verdict`), for any page found in a stored page index: MATCH when the QR
code's full hash located the page and every line matched; MATCH WITH
UNREAD LINES when nothing contradicts the page but some lines (or the QR
code itself) could not be read; MISMATCH when any printed content
contradicts the page; UNKNOWN PAGE only when the page cannot be found at
all (its hash is in no page index the checker may search, or neither the
QR code nor the short hashes could be read). The commitment status
(`CheckResult.commitment_status`), shown separately: ``committed`` when a
recorded evidence commitment covers the page's index or its extract (the
earliest is described, with its proofs), ``not_committed`` when none does
yet, so that the check shows only that the page matches the tool's own
copy.

A hand-made check mark in a contest's box is tolerated (the box's column
is skipped when reading) and detected (`LineResult.marked`) where the box
holds much more ink than the page's other boxes.

`record_check` stores the photo, without its metadata, under its hash in
the workspace and records a ``photo.checked`` event; `lookup_short` finds
pages by typed short hashes, without a photo.

The ``photo.checked`` event (``file_sha256``: the stored photo's hash, or
null when it was not stored; ``county`` and ``board``: the page's) has in
``data``: ``via`` (``cli``, ``coordinator``, ``observer``, ``reader`` or
``public``), ``photo_sha256``, ``stored``, ``size``, ``media_type``,
``received_size`` and ``received_dimensions`` (the photo as uploaded, in
bytes and upright width and height), ``stored_reduced`` (true if the photo
kept is the reduced image as checked, the upload being larger than
`MAX_STORED_PHOTO_BYTES`),
``kind`` (``page`` or ``provenance``), ``located_by`` (``qr``,
``short_hashes`` or null), ``page_sha256``, ``extract_sha256``, ``layout``,
``page``, ``pages``, ``verdict`` and ``content_verdict`` (the same: the
content verdict), ``commitment_status`` (``committed``, ``not_committed``,
or null when no page was found), ``committed`` (a boolean),
``commitment`` (the earliest covering commitment's ``version``, ``time``,
``sums_sha256``, ``bitcoin_block``, ``rekor_log_index`` and ``commit``, or
null), ``counts`` (lines
``matched``, ``differs``, ``unread``, and boxes ``marked``), ``differences``
(each differing line's ``kind``, ballot ``n`` on its board and ``contest``,
never the printed or expected values, which the event log does not need to
carry while the audit is under way) and ``software``.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter


from corla_observer import printlayout
from corla_observer.pulllists import built_list_key
from corla_observer.classify import canonical_county
from corla_observer.events import Event, EventLog, check_actor
from corla_observer.extract import (
    ORDER_LABELS,
    Page,
    board_pages,
    layouts,
    page_of,
    page_index,
    print_marks,
    print_names,
    page_format,
    print_lines,
    provenance,
    provenance_rows,
)
from corla_observer.localtime import colorado_time
from corla_observer.photoimage import (
    Line,
    Photo,
    Prepared,
    QrCode,
    binarize,
    box_interior_ink,
    column_gaps,
    decode_qr,
    flatten,
    grey,
    load_photo,
    ocr,
    ocr_region,
    prepare,
    text_extent,
    workdir,
)
from corla_observer.timestamp import EXTRACT_EVENT_KINDS, parse_sums
from corla_observer.version import software as running_software
from corla_observer.workspace import Workspace


class Verdict:
    MATCH = "MATCH"
    UNREAD = "MATCH WITH UNREAD LINES"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN PAGE"


VERDICTS = (Verdict.MATCH, Verdict.UNREAD, Verdict.MISMATCH, Verdict.UNKNOWN)
#: The command's exit status for each content verdict.
EXIT_CODES = {Verdict.MATCH: 0, Verdict.MISMATCH: 1, Verdict.UNKNOWN: 1, Verdict.UNREAD: 2}
#: The exit status, with ``--require-committed``, for a page no commitment covers yet.
EXIT_NOT_COMMITTED = 1
#: What each way of locating a page means, for people.
LOCATED_BY = {
    "qr": "by its QR code: the full page hash, the check itself",
    "short_hashes": "by the short hashes printed on its identification line only (the QR code could not be read)",
}
#: What "unread" means, for people.
UNREAD_MEANING = (
    "unread: the photo did not show the line clearly enough to tell; it is counted neither as matching nor as differing"
)
#: The ways of reaching the check, recorded in the event.
VIA = ("cli", "coordinator", "observer", "reader", "public")

_PAGE_QR = re.compile(r"^COX2:([0-9A-F]{64})$")
_SCREEN_QR = re.compile(r"^COX2S:([0-9A-F]{32}):(\d{1,2}):(\d{1,5})$")
_EXTRACT_QR = re.compile(r"^COX2E:([0-9A-F]{64})$")
_LEGACY_QR = re.compile(r"^COX1:([0-9A-F]{64}):([0-9A-F]{64})$")
_SHORT = re.compile(r"^[0-9a-f]{4,64}$")

#: The most differences listed in a ``photo.checked`` event.
MAX_EVENT_DIFFERENCES = 60


# ---------------------------------------------------------------------------
# Finding the page


@dataclass(frozen=True)
class ExtractRecord:
    """An extract the workspace built, from its latest ``extract.built`` (or ``practice.extract_built``) event."""

    sha256: str
    county: str
    purpose: str
    seq: int
    page_index_sha256: str | None
    #: Whether it is its county's latest extract of its purpose and pull list.
    latest: bool


@dataclass(frozen=True)
class ScreenLocation:
    """One ballot as the observer screen shows it: its extract, board and place on the board (docs/PHOTO_CHECK.md)."""

    extract: ExtractRecord
    board: int
    n: int
    total: int
    imprinted_id: str

    @property
    def label(self) -> str:
        """``Weld · Board 1 · ballot 41 of 316 · 101-1-67 · extract acbe1a4f · observer screen``."""
        return " · ".join([self.extract.county, f"Board {self.board}", f"ballot {self.n} of {self.total}", self.imprinted_id,
                           f"extract {self.extract.sha256[:8]}", "observer screen"])  # fmt: skip


@dataclass(frozen=True)
class PageLocation:
    extract: ExtractRecord
    layout: str
    board: int
    page: int
    pages: int
    sha256: str

    @property
    def label(self) -> str:
        """Which page it is, as its identification line puts it: ``Weld · Board 1 · page 1 of 21 portrait-2 · extract acbe1a4f``."""
        shown = f" {self.layout}" if self.layout != "portrait" else ""
        parts = [self.extract.county, f"Board {self.board}", f"page {self.page} of {self.pages}{shown}", f"extract {self.extract.sha256[:8]}"]
        return " · ".join(parts)


class PageFinder:
    """Every page of every extract a workspace has built, by hash (see the module documentation)."""

    def __init__(self, workspace: Workspace, log: EventLog) -> None:
        self.workspace = workspace
        records: dict[str, ExtractRecord] = {}
        latest: dict[tuple[str, str, str], str] = {}
        for event in log.iter_events():
            if event.kind in EXTRACT_EVENT_KINDS and event.file_sha256 and event.county:
                purpose = str(event.data.get("purpose") or ("practice" if event.kind.startswith("practice") else "audit"))
                index = event.data.get("page_index_sha256")
                records[event.file_sha256] = ExtractRecord(
                    event.file_sha256, event.county, purpose, event.seq, index if isinstance(index, str) else None, False
                )
                # Each pull list's latest (corla_observer.pulllists): a targeted list never supersedes the RLA sample.
                latest[(event.county, purpose, built_list_key(event))] = event.file_sha256
        current = set(latest.values())
        self.records = {sha: ExtractRecord(**{**asdict(r), "latest": sha in current}) for sha, r in records.items()}
        self._indexes: dict[str, dict[str, Any] | None] = {}
        self._extracts: dict[str, dict[str, Any] | None] = {}

    def extract(self, sha: str) -> dict[str, Any] | None:
        """The extract with this hash, from the workspace, or None."""
        if sha not in self._extracts:
            try:
                data: dict[str, Any] = json.loads(self.workspace.get_bytes(sha))
            except (OSError, ValueError):
                data = None  # type: ignore[assignment]
            self._extracts[sha] = data
        return self._extracts[sha]

    def index(self, record: ExtractRecord) -> dict[str, Any] | None:
        """The page index stored for an extract, or computed from it if none was stored."""
        if record.sha256 not in self._indexes:
            found: dict[str, Any] | None = None
            if record.page_index_sha256:
                try:
                    found = json.loads(self.workspace.get_bytes(record.page_index_sha256))
                except (OSError, ValueError):
                    found = None
            if found is None:
                extract = self.extract(record.sha256)
                found = page_index(extract, record.sha256) if extract else None
            self._indexes[record.sha256] = found
        return self._indexes[record.sha256]

    def pages(self, record: ExtractRecord) -> Iterable[PageLocation]:
        index = self.index(record)
        if not index:
            return
        layouts = index["layouts"] if "layouts" in index else {"portrait": index}
        for layout, part in layouts.items():
            for board, entries in part.get("boards", {}).items():
                for entry in entries:
                    yield PageLocation(record, layout, int(board), int(entry["page"]), len(entries), str(entry["sha256"]))

    def _allowed(self, records: Iterable[ExtractRecord], counties: Sequence[str] | None) -> list[ExtractRecord]:
        return [r for r in records if counties is None or r.county in counties]

    def by_hash(self, sha: str, counties: Sequence[str] | None = None) -> PageLocation | None:
        """The page with this hash, among the extracts of ``counties`` (all if None)."""
        sha = sha.lower()
        for record in sorted(self._allowed(self.records.values(), counties), key=lambda r: -r.seq):
            for location in self.pages(record):
                if location.sha256 == sha:
                    return location
        return None

    def by_short(self, page_short: str, extract_short: str | None = None, counties: Sequence[str] | None = None) -> list[PageLocation]:
        """The pages whose hash starts with ``page_short`` (and whose extract's starts with ``extract_short``)."""
        page_short, extract_short = page_short.lower(), (extract_short or "").lower()
        found: list[PageLocation] = []
        for record in sorted(self._allowed(self.records.values(), counties), key=lambda r: -r.seq):
            if extract_short and not record.sha256.startswith(extract_short):
                continue
            found.extend(loc for loc in self.pages(record) if loc.sha256.startswith(page_short))
        return found

    def by_ident(self, text: str, counties: Sequence[str] | None = None) -> tuple[PageLocation | None, str]:
        """The one page an identification line read with OCR errors names, and how sure; ``(None, why)`` if not exactly one.

        The short hashes are compared allowing for misread characters ("a"
        for "4", "l" for "1"): the page's within 3 edits, the extract's (if
        read) within 3, and the board, page number and page count read must
        agree (the layout is left to the hashes: it is easily misread). The page whose hashes are closest is taken only if
        no other is within two more edits: two random 8-digit hashes are
        that close about once in hundreds of millions.
        """
        extract_read, page_read = ident_hash_tokens(text)
        if page_read is None:
            return None, "no page hash could be read from the identification line"
        fields = ident_fields(text)
        board = re.search(r"\d+", fields.get("board", ""))
        numbers = re.search(r"page (\d+) of (\d+)", fields.get("page", ""))
        scored: list[tuple[int, PageLocation]] = []
        for record in sorted(self._allowed(self.records.values(), counties), key=lambda r: -r.seq):
            d_extract = edits(extract_read, record.sha256[: len(extract_read)]) if extract_read else 0
            if d_extract > 3:
                continue
            for loc in self.pages(record):
                if board and int(board.group()) != loc.board:
                    continue
                if numbers and (int(numbers.group(1)), int(numbers.group(2))) != (loc.page, loc.pages):
                    continue
                d_page = edits(page_read, loc.sha256[: len(page_read)])
                if d_page <= 3:
                    scored.append((d_page + d_extract, loc))
        scored.sort(key=lambda item: item[0])
        if not scored:
            return None, "no page here has the short hashes read from the identification line, even allowing for misread characters"
        if len(scored) > 1 and scored[1][0] - scored[0][0] < 2:
            return None, f"the short hashes read match {len(scored)} pages about equally: the QR code is needed to tell them apart"
        best, location = scored[0]
        how = "exactly" if best == 0 else f"with {best} character{'s' if best > 1 else ''} misread"
        return location, f"the identification line's short hashes, read {how}"

    def screen(self, extract_prefix: str, board: int, n: int, counties: Sequence[str] | None = None) -> ScreenLocation | None:
        """The ballot an observer screen's QR code (or identification) names: the extract whose hash starts with ``extract_prefix``, its board and place."""
        found = [r for r in self._allowed(self.records.values(), counties) if r.sha256.startswith(extract_prefix.lower())]
        if len(found) != 1:
            return None
        extract = self.extract(found[0].sha256)
        if extract is None:
            return None
        ballots = [b for b in extract["ballots"] if int(b["board"]) == board]
        ballot = next((b for b in ballots if int(b["board_position"]) == n), None)
        if ballot is None:
            return None
        return ScreenLocation(found[0], board, n, len(ballots), str(ballot["imprinted_id"]))

    def extract_record(self, sha: str, counties: Sequence[str] | None = None) -> ExtractRecord | None:
        record = self.records.get(sha.lower())
        return record if record and (counties is None or record.county in counties) else None

    def extracts_by_short(self, extract_short: str, counties: Sequence[str] | None = None) -> list[ExtractRecord]:
        return [r for r in self._allowed(self.records.values(), counties) if r.sha256.startswith(extract_short.lower())]


def _columns_of(layout: str) -> int:
    """The columns of ballots on a page of ``layout`` (2 for page format 4's "two-up")."""
    return 2 if layout == "two-up" else printlayout.layout(layout).columns if layout in printlayout.LAYOUTS or layout == "portrait" else 1


def committed_page(finder: PageFinder, location: PageLocation) -> Page | None:
    """The page's content, recomputed from its extract; None if the extract is missing or its hash does not reproduce the indexed one."""
    extract = finder.extract(location.extract.sha256)
    if extract is None:
        return None
    try:
        pages = board_pages(extract, location.extract.sha256, location.board, location.layout)
    except (ValueError, KeyError):
        return None
    if not 1 <= location.page <= len(pages) or pages[location.page - 1].sha256 != location.sha256:
        return None
    return pages[location.page - 1]


# ---------------------------------------------------------------------------
# Commitments


@dataclass(frozen=True)
class Commitment:
    """A recorded evidence commitment covering the page's index or extract, with its proofs."""

    version: int
    sums_name: str
    sums_sha256: str
    seq: int
    time: str
    #: Which of the checked hashes its SUMS file lists ("page index", "extract").
    covers: tuple[str, ...]
    #: The OpenTimestamps proof: "bitcoin" (upgraded to a Bitcoin block), "pending", "skipped", "failed" or "none".
    ots: str
    bitcoin_block: int | None
    rekor_log_index: int | None
    sigstore: str
    commit: str | None
    pushed: bool


def commitments(workspace: Workspace, log: EventLog, wanted: Mapping[str, str]) -> list[Commitment]:
    """The recorded commitments whose SUMS file lists any of ``wanted`` (label to SHA-256), earliest first."""
    found: list[Commitment] = []
    #: Each proof's latest known state: upgraded to Bitcoin (with its block, if recorded), or still pending.
    proof_state: dict[str, tuple[str, int | None]] = {}
    events = list(log.iter_events())
    for event in events:
        if event.kind == "evidence.proofs_upgraded":
            raw_blocks = event.data.get("bitcoin_blocks")
            blocks: dict[str, Any] = raw_blocks if isinstance(raw_blocks, dict) else {}
            upgraded = event.data.get("upgraded")
            still = event.data.get("still_pending")
            still_list = [str(r) for r in still] if isinstance(still, list) else []
            for rel in upgraded if isinstance(upgraded, list) else []:
                height = blocks.get(str(rel))
                if str(rel) not in still_list:
                    proof_state[str(rel)] = ("bitcoin", height if isinstance(height, int) else None)
            for rel in still_list:
                proof_state[rel] = ("pending", None)
    for event in events:
        if event.kind != "evidence.committed" or not event.file_sha256:
            continue
        try:
            sums = parse_sums(workspace.get_bytes(event.file_sha256).decode("utf-8", "replace"))
        except OSError:
            continue
        in_sums = {sha for sha, _ in sums.entries}
        covers = tuple(label for label, sha in wanted.items() if sha in in_sums)
        if not covers:
            continue
        data = event.data
        name = str(data.get("sums_name") or "")
        subdir = str(data.get("subdir") or "")
        rel = f"{subdir}/{name}.ots" if subdir else f"{name}.ots"
        raw_steps = data.get("steps")
        steps = {str(s.get("name")): str(s.get("status")) for s in (raw_steps if isinstance(raw_steps, list) else []) if isinstance(s, dict)}
        ots_step = steps.get("opentimestamps", "none")
        block: int | None = None
        state = proof_state.get(rel)
        if state is not None and state[0] == "bitcoin":
            ots, block = "bitcoin", state[1]
        elif ots_step in ("done", "pending"):
            ots = "pending"
        else:
            ots = ots_step
        rekor = data.get("rekor_log_index")
        found.append(
            Commitment(
                version=version if isinstance(version := data.get("version"), int) else 0,
                sums_name=name,
                sums_sha256=event.file_sha256,
                seq=event.seq,
                time=event.server_time,
                covers=covers,
                ots=ots,
                bitcoin_block=block,
                rekor_log_index=rekor if isinstance(rekor, int) else None,
                sigstore=steps.get("cosign", "none"),
                commit=str(data["commit"]) if data.get("commit") else None,
                pushed=bool(data.get("pushed")),
            )
        )
    return found


# ---------------------------------------------------------------------------
# What the page should show


@dataclass(frozen=True)
class Expected:
    """One line the page should show, as printed."""

    key: str
    #: "ident" (a part of the identification line), "id" (a ballot's imprinted ID line), "marks", "row" (a contest), "fact" (a provenance row).
    kind: str
    text: str
    column: int = 0
    position: int | None = None
    n: int | None = None
    imprinted_id: str | None = None
    contest: str | None = None
    printed_contest: str = ""
    printed_value: str = ""
    #: Every value the line could plausibly show (the contest's printed choices, NO VOTE and the expected value).
    candidates: tuple[str, ...] = ()
    #: For "fact": its label.
    label: str = ""


def _ident_parts(extract: Mapping[str, Any], location: PageLocation) -> list[Expected]:
    parts = [
        ("county", str(extract["county"])),
        ("board", f"Board {location.board}"),
        ("page", page_of(Page(location.board, location.page, location.pages, (), location.sha256, location.layout))),
        ("extract", f"extract {location.extract.sha256[:8]}"),
        ("page_hash", f"page {location.sha256[:8]}"),
    ]
    if extract.get("order") in ORDER_LABELS:
        parts.insert(3, ("order", ORDER_LABELS[str(extract["order"])]))
    return [Expected(f"ident:{name}", "ident", text, label=name) for name, text in parts]


def expected_lines(extract: Mapping[str, Any], location: PageLocation, page: Page) -> list[Expected]:
    """The lines a page should show, in order: the identification line's parts, then per column each ballot's lines (docs/EXTRACT.md)."""
    names = print_names(extract, location.layout)
    choices: dict[str, list[str]] = {str(c): [str(x) for x in v] for c, v in (extract.get("choices") or {}).items()}
    out = _ident_parts(extract, location)
    columns = page.columns or (page.ballots,)
    for col, ballots in enumerate(columns):
        for ballot in ballots:
            position, n, imprinted = int(ballot["position"]), int(ballot["board_position"]), str(ballot["imprinted_id"])
            base = f"b{position}"
            out.append(Expected(f"{base}:id", "id", f"ImprintedId {imprinted}", col, position, n, imprinted))
            marks = print_marks(ballot, names)
            if marks:
                out.append(Expected(f"{base}:marks", "marks", marks, col, position, n, imprinted))
            lines = print_lines(ballot, names, page_format(extract) >= 6)
            votes = ballot["votes"] if ballot["status"] == "votes" else [[None, None]]
            for line in lines:
                contest = votes[line.row][0] if line.row < len(votes) else None
                candidates = [line.value]
                if contest is not None:
                    candidates += [names.vote_lines(c)[0] for c in choices.get(str(contest), [])] + ([] if line.continuation else ["NO VOTE"])
                key = f"{base}:c{line.row}" + (f".{len(out)}" if line.continuation else "")
                shown = f"{' ' * 4}{line.value}" if line.continuation else f"{line.contest} __ {line.value}"
                out.append(
                    Expected(
                        key, "row", shown, col, position, n, imprinted,
                        str(contest) if contest is not None else line.contest, line.contest, line.value,
                        tuple(dict.fromkeys(candidates)),
                    )
                )  # fmt: skip
    return out


def expected_provenance(extract: Mapping[str, Any], sha: str, board: int) -> list[Expected]:
    """The provenance sheet's facts that are the same for every board and layout: county, seed, extract, inputs, page index."""
    rows = provenance_rows(provenance(extract, sha, board))
    wanted = [(label, value) for label, value in rows if label in ("County", "Seed", "Extract", "Page index") or label.startswith("Input: ")]
    return [Expected(f"fact:{label}", "fact", f"{label}  {value}", label=label, printed_value=value) for label, value in wanted]


# ---------------------------------------------------------------------------
# Reading and judging


def norm(text: str) -> str:
    """Lower case, every run of characters other than letters and digits one space."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.lower()).split())


def similarity(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


_DIGIT_LOOKALIKES = str.maketrans({"O": "0", "o": "0", "Q": "0", "D": "0", "l": "1", "I": "1", "i": "1", "|": "1", "!": "1", "S": "5", "s": "5", "Z": "2", "z": "2", "B": "8", "g": "9", "q": "9", "—": "-", "–": "-", "_": "-"})
_HEX_LOOKALIKES = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "i": "1", "|": "1"})


def normal_id(text: str, expected: str) -> str:
    """An imprinted ID as read, normalized like the expected one: stripped of surrounding punctuation, and with look-alike letters as digits if the expected ID is all digits and hyphens."""
    text = text.strip(" .,;:'\"`()[]{}*")
    if re.fullmatch(r"[0-9-]+", expected):
        text = re.sub(r"-+", "-", text.translate(_DIGIT_LOOKALIKES))
        found = re.search(r"[0-9]+(?:-[0-9]+)*", text)
        return found.group(0) if found else ""
    return text


def edits(a: str, b: str) -> int:
    """The edit (Levenshtein) distance between two strings."""
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, start=1):
        current = [i]
        for j, y in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


#: The most characters by which a reading of each kind may differ from what is expected and still be taken for a misreading of it
#: (neither agreeing nor differing): one in an imprinted ID, two in a short hash, four in a full hash.
NEAR_MISS = {"id": 1, "short": 2, "hash": 4}


def near_miss(read: str, expected: str, kind: str) -> bool:
    """Whether a reading differs from the expected value by so little that OCR could have made the difference (`NEAR_MISS`).

    Such a reading is counted neither for nor against the line, so the same
    misreading made twice cannot add up to a difference; a real change that
    small is reported as unread, never as matched.
    """
    return read != expected and edits(read, expected) <= NEAR_MISS[kind]


def padded(read: str, expected: str) -> bool:
    """Whether a reading is the expected ID with a character or two of something else at its ends (a rule beside it, read as text)."""
    return read != expected and expected in read and len(read) - len(expected) <= 2


def well_formed_id(text: str, expected: str) -> bool:
    """Whether a reading looks like an imprinted ID of the expected shape (the same characters and number of parts)."""
    if re.fullmatch(r"[0-9]+(-[0-9]+)*", expected):
        return bool(re.fullmatch(r"[0-9]+(-[0-9]+)*", text)) and text.count("-") == expected.count("-")
    return bool(re.fullmatch(r"[0-9A-Za-z]+(-[0-9A-Za-z]+)*", text))


def _is_header(words: Sequence[str]) -> int | None:
    """The index just after the "ImprintedId" word(s) in a line, if it has them."""
    for i in range(min(len(words), 6)):
        for width in (1, 2):
            joined = re.sub(r"[^a-z]", "", "".join(words[i : i + width]).lower())
            if len(joined) >= 6 and SequenceMatcher(None, joined, "imprintedid").ratio() >= 0.7:
                return i + width
    return None


_COUNTER = re.compile(r"^\d{1,4}[/1l|]\d{1,4}$")
_ID_LIKE = re.compile(r"^\W*[0-9A-Za-z|]*[0-9][0-9A-Za-z|]*(-[0-9A-Za-z|]+)+\W*$")
_RULE = re.compile(r"^[-—–_=~+|tH4]{3,}")


def header_span(words: Sequence[str]) -> tuple[int, int] | None:
    """Where the imprinted ID is among a header line's words (start, end), or None if the line is not a ballot's header.

    A header is a line with the word "ImprintedId", or one that starts with
    the ballot's place on the board ("41/127") and has an ID-like word.
    """
    at = _is_header(words)
    if at is not None:
        end = at
        while end < len(words) and end - at < 3 and not _RULE.match(words[end]) and not words[end].lower().startswith(("loc", "oc")):
            end += 1
        return (at, end) if end > at else None
    if words and _COUNTER.match(words[0]):
        for i, word in enumerate(words[1:6], start=1):
            if _ID_LIKE.match(word) and not _RULE.match(word):
                return (i, i + 1)
    return None


def header_id(words: Sequence[str], expected: str = "0-0") -> str | None:
    """The imprinted ID read from a header line's words, or None."""
    span = header_span(words)
    if span is None:
        return None
    return normal_id("".join(words[span[0] : span[1]]), expected)


@dataclass
class OcrLine:
    """A line of one reading pass, in one column."""

    line: Line
    kind: str  # "id", "marks", "row"
    words: list[str]


def classify(line: Line) -> OcrLine:
    words = [w.text for w in line.words]
    if header_span(words) is not None:
        return OcrLine(line, "id", words)
    first = norm(" ".join(words[:2]))
    if first.startswith(("possible", "boundary")) or words[:1] == ["*"]:
        return OcrLine(line, "marks", words)
    return OcrLine(line, "row", words)


def _line_score(expected: Expected, got: OcrLine) -> float | None:
    if expected.kind == "id":
        if got.kind != "id":
            return None
        read = header_id(got.words, expected.imprinted_id or "")
        return similarity(f"imprintedid {expected.imprinted_id}", f"imprintedid {read or ''}") if read else 0.55
    if got.kind == "id":
        return None
    if expected.kind == "marks":
        return similarity(expected.text, " ".join(got.words))
    return similarity(f"{expected.printed_contest} {expected.printed_value}", " ".join(got.words))


def align(expected: Sequence[Expected], got: Sequence[OcrLine], threshold: float = 0.45) -> dict[int, int]:
    """Pair expected lines with lines read, keeping both orders, maximizing total similarity above ``threshold`` (expected index to read index)."""
    n, m = len(expected), len(got)
    if not n or not m:
        return {}
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    move = [[0] * (m + 1) for _ in range(n + 1)]  # 0 pair, 1 skip expected, 2 skip read
    pair_scores: dict[tuple[int, int], float] = {}
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best, how = score[i - 1][j], 1
            if score[i][j - 1] > best:
                best, how = score[i][j - 1], 2
            s = _line_score(expected[i - 1], got[j - 1])
            if s is not None and s > threshold:
                pair_scores[(i - 1, j - 1)] = s
                if score[i - 1][j - 1] + s - threshold > best:
                    best, how = score[i - 1][j - 1] + s - threshold, 0
            score[i][j], move[i][j] = best, how
    pairs: dict[int, int] = {}
    i, j = n, m
    while i > 0 and j > 0:
        if move[i][j] == 0:
            pairs[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif move[i][j] == 1:
            i -= 1
        else:
            j -= 1
    return pairs


@dataclass
class Reading:
    """What one reading of one expected line showed."""

    #: The normalized value read (an ID, a candidate value, "other:<text>"), or None if nothing usable.
    vote: str | None
    text: str
    source: str
    #: For rows: the box between the contest and the choices, and the choices' extent, in the prepared image.
    box_region: tuple[int, int, int, int] | None = None
    value_box: tuple[int, int, int, int] | None = None
    line_box: tuple[int, int, int, int] | None = None
    #: How much the reading counts: an enlarged re-read of the line alone counts more than a whole-page pass.
    weight: float = 1.0


def _junk(word: str) -> bool:
    """Whether a word is what a check box, or a mark in it, is read as: at most two letters or digits."""
    return len(re.sub(r"[^0-9a-z]", "", word.lower())) <= 2


def _plausible_value(text: str, expected: Expected, contests: Iterable[str]) -> bool:
    """Whether text read where a choice should be could be a printed choice: no longer than the longest possible one by much, and without another contest's name in it (two lines read as one)."""
    longest = max((len(c) for c in expected.candidates), default=0)
    if len(text) > 1.3 * longest + 4:
        return False
    flat = norm(text)
    for contest in contests:
        name = norm(contest)
        if len(name) >= 8 and SequenceMatcher(None, flat, name).find_longest_match(0, len(flat), 0, len(name)).size >= min(len(name), 10):
            return False
    return True


def _row_reading(expected: Expected, got: OcrLine, source: str, contests: Sequence[str] = ()) -> Reading:
    words = got.line.words
    texts = [w.text for w in words]
    contest = norm(expected.printed_contest)
    k_best, s_best = 0, -1.0
    for k in range(0, len(texts) + 1):
        s = SequenceMatcher(None, norm(" ".join(texts[:k])), contest, autojunk=False).ratio() if contest else 0.0
        if s > s_best:
            k_best, s_best = k, s
    best_vote: str | None = None
    best = (-1.0, "", k_best)  # score, text, start of the choices
    scores: dict[str, float] = {}
    # The choices start right after the contest, or after a word or two of what the box (and any mark in it) was read as.
    last = k_best
    while last < len(texts) and last - k_best < 3 and _junk(texts[last]):
        last += 1
    for j in range(k_best, last + 1):
        text = " ".join(texts[j:])
        if not norm(text):
            continue
        for candidate in expected.candidates:
            s = similarity(text, candidate)
            scores[candidate] = max(s, scores.get(candidate, -1.0))
            if s >= best[0]:  # on a tie, the later start: past the box
                best = (s, text, j)
    _, best_text, best_j = best
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if ranked:
        leader, top_score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        if top_score >= 0.8 and top_score - second >= 0.1:
            best_vote = leader
        elif top_score < 0.5 and best_text and got.line.conf >= 80 and s_best >= 0.85 and _plausible_value(best_text, expected, contests) and len(norm(best_text).replace(" ", "")) >= 6:
            best_vote = "other:" + norm(best_text)  # (a word of a few letters is more likely a pen mark read as text than a printed name)
    if best_vote is not None and not best_vote.startswith("other:") and best_vote != expected.printed_value and norm(best_vote) in norm(expected.printed_value):
        best_vote = None  # part of what is expected (one name of an overvote, say): not a contradiction
    box_region = None
    value_box = None
    if 0 < k_best and best_j < len(words) and best_vote is not None:
        contest_right = words[k_best - 1].right
        value_left = words[best_j].left
        top = min(w.top for w in words)
        bottom = max(w.bottom for w in words)
        if value_left > contest_right:
            box_region = (contest_right + 1, top, value_left - 1, bottom)
        value_box = (value_left, top, max(w.right for w in words[best_j:]), bottom)
    elif best_j < len(words):
        value_box = (words[min(best_j, len(words) - 1)].left, min(w.top for w in words), max(w.right for w in words), max(w.bottom for w in words))
    return Reading(best_vote, best_text or " ".join(texts[k_best:]), source, box_region, value_box, got.line.box)


def _id_reading(expected: Expected, got: OcrLine, source: str) -> Reading:
    imprinted = expected.imprinted_id or ""
    read = header_id(got.words, imprinted)
    span = header_span(got.words)
    box = None
    if span is not None:
        words = got.line.words[span[0] : span[1]]
        before = got.line.words[span[0] - 1] if span[0] > 0 and _is_header(got.words) == span[0] else None
        top = min(w.top for w in [*words, *([before] if before else [])])
        bottom = max(w.bottom for w in [*words, *([before] if before else [])])
        box = ((before.right + 2) if before else words[0].left - 2, top, max(w.right for w in words), bottom)
    vote = read if read and well_formed_id(read, imprinted) and not padded(read, imprinted) and not near_miss(read, imprinted, "id") else None
    return Reading(vote, read or "", source, value_box=box, line_box=got.line.box)


def _judge_votes(expected_vote: str, readings: Sequence[Reading], *, group: bool = False) -> tuple[str, str | None]:
    """``(status, the differing value read)`` from the readings' votes: see the module documentation."""
    agree = 0.0
    others: dict[str, float] = {}
    for r in readings:
        v = r.vote
        if v is None:
            continue
        if v == expected_vote:
            agree += r.weight
            continue
        if group and v.startswith("other:"):
            twin = next((o for o in others if o.startswith("other:") and similarity(o[6:], v[6:]) >= 0.8), None)
            v = twin or v
        others[v] = others.get(v, 0.0) + r.weight
    top, top_count = max(others.items(), key=lambda kv: kv[1]) if others else (None, 0.0)
    if agree and agree > top_count:
        return "matched", None
    if not agree and top_count >= 2:
        return "differs", top
    return "unread", top


@dataclass
class LineResult:
    key: str
    kind: str
    expected: str
    status: str  # "matched", "differs", "unread"
    read: str | None = None
    detail: str = ""
    marked: bool = False
    column: int = 0
    n: int | None = None
    imprinted_id: str | None = None
    contest: str | None = None
    #: For a line not matched: why (`why_not_matched`), and every reading of it, pass by pass (`reading_record`).
    reason: str = ""
    reads: list[dict[str, Any]] = field(default_factory=list)
    #: Where the line was found in the prepared (upright, straightened) image, if it was.
    box: tuple[int, int, int, int] | None = None


def reading_record(r: Reading, expected_vote: str) -> dict[str, Any]:
    """One reading for the result: the pass, the text read, and how it was judged ("as expected", "other", or "not usable")."""
    judged = "not usable" if r.vote is None else "as expected" if r.vote == expected_vote else "other"
    return {"pass": r.source, "text": r.text, "judged": judged, "value": r.vote if judged == "other" else None}


def why_not_matched(status: str, expected_vote: str, readings: Sequence[Reading]) -> str:
    """In words, why a line is unread or differs, from its readings' votes (`_judge_votes`)."""
    if status == "matched":
        return ""
    if not readings:
        return "nothing found where the line should be"
    agree = sum(r.weight for r in readings if r.vote == expected_vote)
    others = [r for r in readings if r.vote is not None and r.vote != expected_vote]
    if status == "differs":
        return "two or more readings agree on something else, and none read it as committed"
    if agree and others:
        return "the readings disagree: some read it as committed, as many or more read something else"
    if others:
        return "read as something else only once: not enough to say it differs"
    if any(r.text for r in readings):
        return "no reading was clear enough to judge (too garbled, or too near the committed text to count either way)"
    return "found, but nothing could be read from it"


# ---------------------------------------------------------------------------
# The identification line


_HEX8 = re.compile(r"^[0-9a-f]{8}$")
#: The characters a short hash is read as when re-read on its own (either case: the result is lowered).
HEX_WHITELIST = "0123456789abcdefABCDEF"


def ident_fields(text: str) -> dict[str, str]:
    """The parts of an identification line as read: board, page ("N of M" and the layout), order, the extract's and page's short hashes, county."""
    fields: dict[str, str] = {}
    flat = " ".join(text.replace("·", " ").split())
    lower = flat.lower()
    m = re.search(r"board\W{0,2}([0-9oOlI]{1,2})\b", flat, re.IGNORECASE)
    if m:
        fields["board"] = f"Board {m.group(1).translate(_DIGIT_LOOKALIKES)}"
    m = re.search(r"page\W{0,2}([0-9oOlI]{1,4})\W{0,2}of\W{0,2}([0-9oOlI]{1,4})(\W{0,3}(portrait|landscape)\W{0,2}([0-9oOlIZz]))?", flat, re.IGNORECASE)
    if m:
        shown = f" {m.group(4).lower()}-{m.group(5).translate(_DIGIT_LOOKALIKES)}" if m.group(3) else ""
        fields["page"] = f"page {m.group(1).translate(_DIGIT_LOOKALIKES)} of {m.group(2).translate(_DIGIT_LOOKALIKES)}" + shown
    m = re.search(r"extract\W{0,3}([0-9a-zA-Z|]{7,10})\b", flat, re.IGNORECASE)
    if m:
        value = m.group(1).translate(_HEX_LOOKALIKES).lower()
        if _HEX8.match(value):
            fields["extract"] = f"extract {value}"
    tail = re.findall(r"page\W{0,3}([0-9a-zA-Z|]{7,10})\b", flat, re.IGNORECASE)
    for value in reversed(tail):
        value = value.translate(_HEX_LOOKALIKES).lower()
        if _HEX8.match(value):
            fields["page_hash"] = f"page {value}"
            break
    for label in ORDER_LABELS.values():
        if similarity(label, flat) and label.lower().split()[0] in lower:
            fields["order"] = label
    first = re.split(r"board", flat, maxsplit=1, flags=re.IGNORECASE)[0]
    # Before "Board": the county, perhaps after junk read off the page's edge or a screen's border (": | _ Weld").
    words = [w.strip(" +·-«».:;,|_'\"`()[]{}") for w in first.split()]
    words = [w for w in words if re.search(r"[A-Za-z]{2}", w)]
    for n in (3, 2, 1):
        county = canonical_county(" ".join(words[-n:])) if len(words) >= n else None
        if county is not None:
            fields["county"] = county
            break
    else:
        text = " ".join(words)
        if text and len(text) <= 30:
            fields["county"] = text
    return fields


def _ident_score(text: str) -> int:
    low = text.lower()
    score = 2 * ("board" in low) + 2 * ("extract" in low) + bool(re.search(r"page\W{0,2}[0-9oOlI]", low))
    score += bool(re.search(r"\bof\b", low)) + ("two" in low and "up" in low or "portrait" in low or "landscape" in low) + ("order" in low)
    return score


def find_ident(lines: Sequence[Line]) -> Line | None:
    """The identification line among the lines read, wherever it is: the one most like it ("Board", "extract", "page N of M", the layout, the order), the first of equals, joined with the next line if it was printed on two; None if none has "Board" with "page" or "extract"."""
    best: tuple[int, int] | None = None
    for n, line in enumerate(lines):
        score = _ident_score(line.text)
        if score >= 3 and "board" in line.text.lower() and (best is None or score > best[0]):
            best = (score, n)
    if best is None:
        return None
    n = best[1]
    line = lines[n]
    # Printed on two lines (too long beside the QR code), its second line has the hashes, or the page's alone
    # ("Weld · Board 1 · page 1 of 21 portrait-2 · AUDIT CENTER ORDER · extract acbe1a4f", then "page a631fade"): read as one.
    if n + 1 < len(lines):
        following = lines[n + 1]
        extract, page = ident_hash_tokens(line.text)
        _, next_page = ident_hash_tokens(following.text)
        if ("extract" not in line.text.lower() and "extract" in following.text.lower()) or (page is None and next_page is not None):
            return Line([*line.words, *following.words])
    return line


def ident_hash_tokens(text: str) -> tuple[str | None, str | None]:
    """The extract's and the page's short hashes as read from an identification line, look-alike letters as hex digits, whether or not they read as 8 hex digits ("acbeladf", "651148148")."""
    flat = " ".join(text.replace("·", " ").split())

    def clean(token: str) -> str | None:
        value = token.translate(_HEX_LOOKALIKES).lower()
        return value if 6 <= len(value) <= 10 and sum(ch in "0123456789abcdef" for ch in value) >= len(value) - 2 else None

    extract = None
    m = re.search(r"extract\W{0,3}([0-9a-zA-Z|]{6,10})", flat, re.IGNORECASE)
    if m:
        extract = clean(m.group(1))
    page = None
    for token in reversed(re.findall(r"page\W{0,3}([0-9a-zA-Z|]{6,10})", flat, re.IGNORECASE)):
        page = clean(token)
        if page:
            break
    return extract, page


# ---------------------------------------------------------------------------
# The check


@dataclass
class CheckResult:
    verdict: str
    summary: str
    kind: str | None = None  # "page" or "provenance"
    located_by: str | None = None  # "qr" or "short_hashes"
    qr: list[str] = field(default_factory=list)
    location: PageLocation | None = None
    #: For a screenshot or photo of the observer screen: the ballot it shows.
    screen: ScreenLocation | None = None
    extract_sha256: str | None = None
    county: str | None = None
    board: int | None = None
    lines: list[LineResult] = field(default_factory=list)
    commitments: list[Commitment] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    photo_sha256: str | None = None
    media_type: str | None = None
    size: int = 0
    #: For each line not matched that was found: a small PNG of it, cut from the prepared image, to judge by eye (never recorded).
    crops: dict[str, bytes] = field(default_factory=dict, repr=False)

    @property
    def committed(self) -> bool:
        return bool(self.commitments)

    @property
    def found(self) -> bool:
        """Whether the page (or the provenance sheet's extract) was found in a stored index."""
        return self.location is not None or self.screen is not None or (self.kind == "provenance" and self.extract_sha256 is not None)

    @property
    def commitment_status(self) -> str | None:
        """``committed``, ``not_committed``, or None when nothing was found to be committed."""
        if not self.found:
            return None
        return "committed" if self.commitments else "not_committed"

    def commitment_text(self) -> str:
        """The commitment status in words: the earliest covering commitment and its proofs, or why there is none."""
        if not self.found:
            return ""
        if not self.commitments:
            what = {"provenance": "this extract is in the workspace", "screen": "this ballot is in the workspace's extract"}.get(
                self.kind or "", "this page is in the workspace's extract"
            )
            return (
                f"NOT YET COMMITTED: {what} but no published, timestamped commitment covers it yet, "
                "so this check shows only that it matches the tool's own copy"
            )
        c = self.commitments[0]
        proofs = []
        if c.ots == "bitcoin":
            proofs.append(f"Bitcoin block {c.bitcoin_block}" if c.bitcoin_block is not None else "in a Bitcoin block")
        elif c.ots == "pending":
            proofs.append("Bitcoin timestamp pending")
        if c.rekor_log_index is not None:
            proofs.append(f"Rekor {c.rekor_log_index}")
        if c.commit:
            proofs.append(f"commit {c.commit[:12]}")
        more = f" (and {len(self.commitments) - 1} later)" if len(self.commitments) > 1 else ""
        return f"committed in evidence version {c.version} at {colorado_time(c.time, seconds=False)}" + "".join(f", {p}" for p in proofs) + more

    def located_text(self) -> str:
        """How the page was located, in words."""
        if self.kind == "screen":
            return {"qr": "by the ballot's QR code on the screen: the extract's hash, the board and the ballot's place",
                    "short_hashes": "by the extract's short hash and \"Ballot n of N\" read on the screen (the QR code could not be read)"}.get(
                self.located_by or "", "not located")  # fmt: skip
        return LOCATED_BY.get(self.located_by or "", "not located: the page could not be identified")

    def exit_code_for(self, require_committed: bool = False) -> int:
        """The command's exit status: by the content verdict, and nonzero for an uncommitted page if ``require_committed``."""
        code = EXIT_CODES[self.verdict]
        if require_committed and code == 0 and self.commitment_status != "committed":
            return EXIT_NOT_COMMITTED
        return code

    @property
    def exit_code(self) -> int:
        return self.exit_code_for()

    def counts(self) -> dict[str, int]:
        statuses = Counter(line.status for line in self.lines)
        return {
            "matched": statuses["matched"],
            "differs": statuses["differs"],
            "unread": statuses["unread"],
            "marked": sum(1 for line in self.lines if line.marked),
        }

    def differences(self) -> list[LineResult]:
        return [line for line in self.lines if line.status == "differs"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "summary": self.summary,
            "kind": self.kind,
            "located_by": self.located_by,
            "qr": list(self.qr),
            "page": asdict(self.location) if self.location else None,
            "screen": asdict(self.screen) if self.screen else None,
            "extract_sha256": self.extract_sha256,
            "county": self.county,
            "board": self.board,
            "content_verdict": self.verdict,
            "commitment_status": self.commitment_status,
            "commitment_text": self.commitment_text(),
            "committed": self.committed,
            "commitments": [asdict(c) for c in self.commitments],
            "counts": self.counts(),
            "lines": [asdict(line) for line in self.lines],
            "reasons": list(self.reasons),
            "notes": list(self.notes),
            "photo_sha256": self.photo_sha256,
            "media_type": self.media_type,
            "size": self.size,
        }


def parse_screen_qr(codes: Iterable[QrCode | str]) -> list[tuple[str, int, int]]:
    """The observer screen's QR codes among those read: ``(extract hash prefix, board, board position)`` (`corla_observer.extract.screen_qr_payload`)."""
    found: list[tuple[str, int, int]] = []
    for code in codes:
        data = code.data if isinstance(code, QrCode) else code
        if m := _SCREEN_QR.match(data):
            found.append((m.group(1).lower(), int(m.group(2)), int(m.group(3))))
    return list(dict.fromkeys(found))


def parse_qr(codes: Iterable[QrCode | str]) -> tuple[list[str], list[str], list[str]]:
    """The page hashes, extract hashes and other texts among the QR codes read."""
    pages: list[str] = []
    extracts: list[str] = []
    other: list[str] = []
    for code in codes:
        data = code.data if isinstance(code, QrCode) else code
        if m := _PAGE_QR.match(data):
            pages.append(m.group(1).lower())
        elif m := _LEGACY_QR.match(data):
            pages.append(m.group(1).lower())
        elif m := _EXTRACT_QR.match(data):
            extracts.append(m.group(1).lower())
        else:
            other.append(data)
    return list(dict.fromkeys(pages)), list(dict.fromkeys(extracts)), other


class _Reader:
    """The reading passes over one prepared page image."""

    def __init__(self, prepared: Prepared, work: Path, columns: int = 1) -> None:
        self.prepared = prepared
        self.work = work
        self.image = prepared.image
        self.columns = columns
        self.passes: list[tuple[str, Line | None, list[list[OcrLine]]]] = []
        self.first_lines: list[Line] = []
        self.gaps: list[int] | None = None

    def _columns(self, lines: Sequence[Line], gaps: Sequence[int] | None, ident: Line | None) -> list[list[OcrLine]]:
        """The lines read, below the identification line, in the page's columns.

        tesseract may join lines of neighbouring columns that are level with
        each other into one line; each line is split at its widest space near
        each boundary between columns, or, without one there, at the
        boundary found for the page (`column_gaps`, else where it should
        be). A photo taken at an angle has no single straight dividing line,
        so the split is made line by line, not at one x for the page.
        """
        body = list(lines)
        if ident is not None:
            # The identification line may be two lines read as one (`find_ident`): neither is a line of the body.
            in_ident = {id(w) for w in ident.words}
            body = [ln for ln in body if ln.box[1] > ident.box[1] and not any(id(w) in in_ident for w in ln.words)]
        n = self.columns
        if n < 2:
            return [[classify(ln) for ln in body]]
        width = self.image.width
        found: dict[int, dict[int, int]] = {}  # boundary -> line -> x
        for i, ln in enumerate(body):
            words = sorted(ln.words, key=lambda w: w.left)
            for k in range(1, n):
                expected = width * k / n
                best = None
                for a, b in zip(words, words[1:]):
                    space = b.left - a.right
                    centre = (a.right + b.left) / 2
                    if space > 0.012 * width and abs(centre - expected) < 0.4 * width / n and (best is None or space > best[0]):
                        best = (space, round(centre))
                if best is not None:
                    found.setdefault(k, {})[i] = best[1]
        if gaps is not None and len(gaps) == n - 1:
            bounds = list(gaps)
        else:
            bounds = [round(statistics.median(found[k].values())) if found.get(k) else round(width * k / n) for k in range(1, n)]
        out: list[list[OcrLine]] = [[] for _ in range(n)]
        for i, ln in enumerate(body):
            splits = [found.get(k, {}).get(i, bounds[k - 1]) for k in range(1, n)]
            parts: list[list[Any]] = [[] for _ in range(n)]
            for w in ln.words:
                centre = (w.left + w.right) / 2
                parts[sum(1 for x in splits if centre >= x)].append(w)
            for c, words in enumerate(parts):
                if words:
                    out[c].append(classify(Line(words)))
        for column in out:
            column.sort(key=lambda o: o.line.box[1])
        return out

    def run(self) -> None:
        # Pass 1: as photographed (grey, level), the whole page as one block.
        lines = ocr(self.image, self.work, psm=6, name="pass1")
        self.first_lines = lines
        ident = find_ident(lines)
        top = ident.box[3] if ident else 0
        gaps = column_gaps(self.image, top, self.columns) if self.columns > 1 else None
        self.gaps = gaps
        self.passes.append(("pass 1", ident, self._columns(lines, gaps, ident)))
        # Pass 2: flattened against glare and shadow; the columns of a page of several read separately.
        flat = flatten(self.image)
        if self.columns > 1 and gaps:
            columns: list[list[OcrLine]] = []
            edges = [0, *gaps, self.image.width]
            for n, (x0, x1) in enumerate(zip(edges, edges[1:])):
                crop = flat.crop((x0, top, x1, self.image.height))
                got = ocr(crop, self.work, psm=4, name=f"pass2-{n}")
                shifted = [Line([type(w)(w.text, w.conf, w.left + x0, w.top + top, w.width, w.height) for w in ln.words]) for ln in got]
                columns.append([classify(ln) for ln in shifted])
            head = ocr(flat.crop((0, 0, self.image.width, max(1, top + 4))), self.work, psm=6, name="pass2-head") if top else []
            ident2 = find_ident(head)
            self.passes.append(("pass 2", ident2, columns))
        else:
            got = ocr(flat, self.work, psm=4, name="pass2")
            ident2 = find_ident(got)
            self.passes.append(("pass 2", ident2, self._columns(got, gaps, ident2)))

    def reread(
        self, box: tuple[int, int, int, int] | None, whitelist: str | None = None, factor: float = 2.5, binary: bool = False, tight: bool = False
    ) -> Line | None:
        if box is None:
            return None
        image = binarize(self.image) if binary else self.image
        return ocr_region(image, box, self.work, whitelist=whitelist, factor=factor, tight=tight)


def _compare_page(reader: _Reader, expected: Sequence[Expected], all_ids: Mapping[str, int]) -> list[LineResult]:
    readings: dict[str, list[Reading]] = {e.key: [] for e in expected}
    body = [e for e in expected if e.kind != "ident"]
    contests = sorted({e.printed_contest for e in body if e.kind == "row" and e.printed_contest})
    ncols = max((e.column for e in body), default=0) + 1
    extras: list[tuple[str, str, OcrLine]] = []  # (pass, kind, line)
    for name, ident, columns in reader.passes:
        if ident is not None:
            for e in expected:
                if e.kind == "ident":
                    value = ident_fields(ident.text).get(e.label)
                    vote = None if value and e.label in ("extract", "page_hash") and near_miss(value, e.text, "short") else value
                    if value and e.label == "county" and near_miss(value.lower(), e.text.lower(), "id"):
                        vote = None  # a letter misread in the county's name ("Wild" for "Weld")
                    readings[e.key].append(Reading(vote, value or "", name, line_box=ident.box))
        groups: list[tuple[list[Expected], list[OcrLine]]]
        if len(columns) == ncols:
            groups = [([e for e in body if e.column == c], columns[c]) for c in range(ncols)]
        else:
            groups = [(list(body), [o for col in columns for o in col])]
        for exp, got in groups:
            pairs = align(exp, got)
            used = set(pairs.values())
            for i, j in pairs.items():
                e, o = exp[i], got[j]
                if e.kind == "id":
                    readings[e.key].append(_id_reading(e, o, name))
                elif e.kind == "marks":
                    s = similarity(e.text, " ".join(o.words))
                    readings[e.key].append(Reading(e.text if s >= 0.75 else None, " ".join(o.words), name, line_box=o.line.box))
                else:
                    readings[e.key].append(_row_reading(e, o, name, contests))
            for j, o in enumerate(got):
                if j not in used:
                    extras.append((name, o.kind, o))

    by_key = {e.key: e for e in expected}
    # Read again, enlarged, every line not yet settled.
    rereads = 0
    for e in expected:
        status, _ = _status(e, readings[e.key])
        if status == "matched" or rereads >= 80:
            continue
        boxes = [r.value_box for r in readings[e.key] if r.value_box] or [r.line_box for r in readings[e.key] if r.line_box]
        if not boxes:
            continue
        box = boxes[0]
        if e.kind == "id":
            assert e.imprinted_id is not None
            height = box[3] - box[1]
            tight = text_extent(reader.image, box, reach=2 * height) or box
            chars = "".join(sorted(set(e.imprinted_id + "0123456789-")))
            for whitelist, binary, factor in ((chars, False, 0.0), (chars, True, 0.0), (None, False, 0.0)):
                line = reader.reread(tight, whitelist=whitelist, binary=binary, factor=factor, tight=True)
                rereads += 1
                if line and line.words:
                    again = normal_id("".join(w.text for w in line.words), e.imprinted_id)
                    vote = again if well_formed_id(again, e.imprinted_id) and not near_miss(again, e.imprinted_id, "id") else None
                    readings[e.key].append(Reading(vote, again, "re-read", weight=1.5))
        elif e.kind == "row":
            for binary in (False, True):
                line = reader.reread(box, binary=binary)
                rereads += 1
                if line and line.words:
                    readings[e.key].append(_value_vote(e, " ".join(w.text for w in line.words), line.conf, "re-read", contests))
        elif e.kind == "ident" and e.label in ("extract", "page_hash"):
            # The short hash alone, cut out of the identification line, read as hex.
            prefix = "extract" if e.label == "extract" else "page"
            for _, ident, _ in reader.passes:
                if ident is None:
                    continue
                words = ident.words
                # The word after the label, if it could be the hash ("page 1 of 21" is not: its "page" is followed by a number).
                at = [i for i, w in enumerate(words[:-1]) if similarity(w.text, prefix) >= 0.75 and len(words[i + 1].text) >= 6]
                if not at:
                    continue
                word = words[at[-1] + 1]
                # As photographed, black and white, and black and white enlarged further (to about 160 pixels high), each read as hex only.
                enlarged = min(6.0, max(1.5, 160 / max(1, word.bottom - word.top)))
                for binary, factor in ((False, 0.0), (True, 0.0), (True, enlarged)):
                    line = reader.reread((word.left, word.top, word.right, word.bottom), whitelist=HEX_WHITELIST, factor=factor, binary=binary, tight=True)
                    rereads += 1
                    text = "".join(w.text for w in line.words).lower() if line else ""
                    value = f"{prefix} {text}" if _HEX8.match(text) and not near_miss(f"{prefix} {text}", e.text, "short") else None
                    readings[e.key].append(Reading(value, text, "re-read", weight=1.5))
                break
        elif e.kind == "ident":
            line = reader.reread(box, factor=0.0)
            rereads += 1
            if line and line.words:
                value = ident_fields(line.text).get(e.label)
                readings[e.key].append(Reading(value, value or "", "re-read"))

    results: list[LineResult] = []
    for e in expected:
        status, other = _status(e, readings[e.key])
        read: str | None = None
        detail = ""
        if status != "matched":
            read = other[6:] if other and other.startswith("other:") else other
            if read is None:
                texts = [r.text for r in readings[e.key] if r.text]
                read = texts[0] if texts else None
        if status == "differs" and e.kind == "id" and other is not None and other in all_ids:
            detail = f"this is the imprinted ID of ballot {all_ids[other]} on this board"
        if status == "unread" and not readings[e.key]:
            detail = "not found on the photo"
        line_result = LineResult(e.key, e.kind, e.text, status, read, detail, column=e.column, n=e.n, imprinted_id=e.imprinted_id, contest=e.contest)
        if status != "matched":
            vote = _expected_vote(e)
            line_result.reason = why_not_matched(status, vote, readings[e.key])
            line_result.reads = [reading_record(r, vote) for r in readings[e.key]]
            line_result.box = next((r.line_box or r.value_box for r in readings[e.key] if r.line_box or r.value_box), None)
        results.append(line_result)
    results.extend(_extras(extras, by_key, reader.passes, all_ids))
    _distrust_scattered_rows(results)
    _detect_marks(reader, expected, readings, results)
    return results


def _distrust_scattered_rows(results: Sequence[LineResult]) -> None:
    """Choices that seem to differ on a page read too poorly to tell are unread, not differing.

    When the photo was read so badly that differing contest lines are more
    than a quarter of the matched ones, and nothing firmer differs (an
    imprinted ID, the identification line, a line not on the page), the
    differences more likely come from lines read into one another than
    from the paper: they are reported as unread. A real alteration is then
    MATCH WITH UNREAD LINES, never MATCH.
    """
    rows = [r for r in results if r.kind == "row"]
    differing = [r for r in rows if r.status == "differs"]
    matched = sum(1 for r in rows if r.status == "matched")
    firm = any(r.status == "differs" and r.kind in ("id", "ident", "extra") for r in results)
    if differing and not firm and matched < 4 * len(differing):
        for r in differing:
            r.status = "unread"
            r.detail = "the photo was read too poorly to tell this line apart from its neighbours"


def _value_vote(e: Expected, text: str, conf: float, source: str, contests: Sequence[str] = ()) -> Reading:
    """A re-read of a line's choices, judged like `_row_reading`'s."""
    scores = sorted(((similarity(text, c), c) for c in e.candidates), reverse=True)
    vote = None
    if scores:
        top_score, top = scores[0]
        second = scores[1][0] if len(scores) > 1 else 0.0
        if top_score >= 0.8 and top_score - second >= 0.1:
            vote = top
            if top != e.printed_value and norm(top) in norm(e.printed_value):
                vote = None
        elif top_score < 0.5 and conf >= 80 and len(norm(text).replace(" ", "")) >= 6 and _plausible_value(text, e, contests):
            vote = "other:" + norm(text)
    return Reading(vote, text, source, weight=1.5)


def _expected_vote(e: Expected) -> str:
    """The vote that agrees with ``e`` (as `_status` judges it)."""
    if e.kind == "id":
        return e.imprinted_id or ""
    if e.kind in ("marks", "ident"):
        return e.text
    if e.kind == "fact":
        return _fact_value(e.printed_value)
    return e.printed_value


def _status(e: Expected, readings: Sequence[Reading]) -> tuple[str, str | None]:
    if e.kind == "id":
        return _judge_votes(e.imprinted_id or "", readings)
    if e.kind == "row":
        return _judge_votes(e.printed_value, readings, group=True)
    if e.kind == "marks":
        return ("matched", None) if any(r.vote == e.text for r in readings) else ("unread", None)
    if e.kind == "ident":
        return _judge_votes(e.text, readings)
    return _judge_votes(e.printed_value, readings)


def _extras(extras: Sequence[tuple[str, str, OcrLine]], by_key: Mapping[str, Expected], passes: Sequence[Any], all_ids: Mapping[str, int]) -> list[LineResult]:
    """Lines read but not expected, that two passes agree on: another ballot's imprinted ID, or a contest line."""
    out: list[LineResult] = []
    page_ids = {e.imprinted_id for e in by_key.values() if e.kind == "id"}
    sample = next((i for i in page_ids if i), "0-0")
    ids: Counter[str] = Counter()
    rows: list[tuple[str, str]] = []
    contests = {norm(e.printed_contest) for e in by_key.values() if e.kind == "row" and e.printed_contest}
    # A line read as a contest of this page with (nearly) that row's choice's last word at its end, garbled before it (a pen mark over the box, read as text): a reading of that row, not another line.
    page_rows = [(norm(e.printed_contest), norm(e.printed_value).split()[-1]) for e in by_key.values() if e.kind == "row" and e.printed_contest and norm(e.printed_value)]
    for name, kind, line in extras:
        if kind == "id":
            read = header_id(line.words, sample)
            if read and well_formed_id(read, sample) and read not in page_ids:
                ids[read] += 1
        elif kind == "row" and line.line.conf >= 70:
            text = " ".join(line.words)
            flat = norm(text)
            tail = flat.split()[-1] if flat.split() else ""
            if any(similarity(flat[: len(c) + 2], c) >= 0.85 and SequenceMatcher(None, tail, last, autojunk=False).ratio() >= 0.6 for c, last in page_rows):
                continue
            # A contest's name with nothing after it but what a box or a mark is read as: no choice to contradict anything.
            if any(similarity(flat[: len(c) + 2], c) >= 0.85 and not [w for w in flat[len(c) :].split() if not _junk(w)] for c in contests):
                continue
            if any(similarity(text[: len(c) + 4], c) >= 0.85 for c in contests):
                rows.append((name, text))
    for read, count in ids.items():
        if count >= 2:
            where = f"; it is ballot {all_ids[read]}'s on this board" if read in all_ids else ""
            out.append(LineResult(f"extra:id:{read}", "extra", "", "differs", f"ImprintedId {read}", f"a ballot that is not on this page{where}"))
    seen: list[str] = []
    for name, text in rows:
        if any(similarity(text, s) >= 0.85 for s in seen):
            continue
        twins = {n for n, t in rows if similarity(t, text) >= 0.85}
        if len(twins) >= 2:
            seen.append(text)
            out.append(LineResult(f"extra:row:{len(seen)}", "extra", "", "differs", text, "a contest line that is not on this page"))
    return out


def _detect_marks(reader: _Reader, expected: Sequence[Expected], readings: Mapping[str, Sequence[Reading]], results: list[LineResult]) -> None:
    """Flag the boxes holding much more ink than the page's typical box: a hand-made check mark.

    The box is looked for between its contest's name and its choices on the
    first pass, on the page flattened against glare and shadow, and the ink
    inside its outline measured (`box_interior_ink`): a box is marked if it
    holds clearly more than an empty one (over 22 percent of its inside
    dark), and over 1.6 times the page's typical box.
    """
    regions: dict[str, tuple[int, int, int, int]] = {}
    for e in expected:
        if e.kind != "row":
            continue
        region = next((r.box_region for r in readings[e.key] if r.source == "pass 1" and r.box_region), None)
        if region is not None:
            regions[e.key] = region
    if len(regions) < 8:
        return
    flat = flatten(reader.image)
    measured = {key: value for key, r in regions.items() if (value := box_interior_ink(flat, r)) is not None}
    if len(measured) < 8:
        return
    median = statistics.median(measured.values())
    by_key = {r.key: r for r in results}
    for key, value in measured.items():
        if value > max(0.22, 1.6 * median):
            by_key[key].marked = True


def _verdict(result: CheckResult) -> None:
    counts = result.counts()
    if result.location is None and result.kind not in ("provenance", "screen"):
        result.verdict = Verdict.UNKNOWN
    elif counts["differs"]:
        result.verdict = Verdict.MISMATCH
        result.reasons.insert(0, f"{counts['differs']} printed line(s) contradict the page as the workspace holds it")
    elif result.located_by == "qr" and counts["unread"] == 0 and counts["matched"]:
        result.verdict = Verdict.MATCH
    else:
        result.verdict = Verdict.UNREAD
        if result.located_by != "qr":
            what = "ballot" if result.kind == "screen" else "page"
            result.reasons.append(f"the QR code could not be read: the {what} was located by its short hashes only")
        if counts["unread"]:
            result.reasons.append(f"{counts['unread']} line(s) could not be read")
    what = {"provenance": "provenance sheet", "screen": "observer screen showing"}.get(result.kind or "", "page")
    place = (
        result.location.label
        if result.location
        else result.screen.label.removesuffix(" · observer screen")
        if result.screen
        else (f"{result.county} · extract {result.extract_sha256[:8]}" if result.extract_sha256 else "")
    )
    result.summary = f"{result.verdict}: {what} {place}".rstrip() + (
        f" ({counts['matched']} matched, {counts['differs']} differ, {counts['unread']} unread"
        + (f", {counts['marked']} marked" if counts["marked"] else "")
        + ")"
        if result.lines
        else ""
    )
    if result.commitment_status == "not_committed":
        result.summary += "; NOT YET COMMITTED"
    elif result.commitment_status == "committed":
        result.summary += f"; committed in evidence version {result.commitments[0].version}"


def check_photo(
    workspace: Workspace,
    log: EventLog,
    photo: Photo | bytes,
    *,
    counties: Sequence[str] | None = None,
    work_parent: Path | None = None,
    finder: PageFinder | None = None,
) -> CheckResult:
    """Check a photo or scan of a printed extract page against the workspace's committed extracts (see the module documentation).

    ``counties`` limits the extracts searched (an observer's own county).

    Raises:
        PhotoError: the image is not an accepted image.
        ToolMissingError: zbarimg or tesseract is not installed.
    """
    if isinstance(photo, bytes):
        photo = load_photo(photo)
    finder = finder or PageFinder(workspace, log)
    result = CheckResult(Verdict.UNKNOWN, "", photo_sha256=hashlib.sha256(photo.clean).hexdigest(), media_type=photo.media_type, size=len(photo.clean))
    scope = "the extracts you can check" if counties is not None else "this workspace's extracts"
    with workdir(work_parent) as work:
        codes = decode_qr(photo.image, work)
        result.qr = [c.data for c in codes]
        pages, extracts, other = parse_qr(codes)
        screens = parse_screen_qr(codes)
        other = [o for o in other if not _SCREEN_QR.match(o)]
        if screens and not pages and not extracts:
            if len(screens) > 1:
                result.reasons.append(f"{len(screens)} different ballots' QR codes in one photo: photograph one ballot at a time")
                _verdict(result)
                return result
            return _check_screen(workspace, log, finder, result, photo, screens[0], codes[0].orientation if codes else None, work, counties, scope)
        if other:
            result.notes.append(f"{len(other)} other QR code(s) ignored")
        if len(pages) > 1:
            result.reasons.append(f"{len(pages)} different page QR codes in one photo: photograph one page at a time")
            _verdict(result)
            return result
        location: PageLocation | None = None
        if pages:
            location = finder.by_hash(pages[0], counties)
            result.located_by = "qr"
            if location is None:
                result.reasons.append(f"the QR code's page hash {pages[0][:8]}… is in none of {scope}")
                _verdict(result)
                return result
        orientation = codes[0].orientation if codes else None
        prepared = prepare(photo.image, work, orientation, columns=_columns_of(location.layout) if location is not None else 1)
        if location is None and extracts:
            return _check_provenance(workspace, log, finder, result, extracts[0], prepared, work, counties, scope, by_qr=True)
        if location is None:
            ident = _find_ident_line(prepared.image, work)
            fields = ident_fields(ident.text) if ident else {}
            page_short = fields.get("page_hash", "")[5:]
            extract_short = fields.get("extract", "")[8:]
            if ident is not None and extract_short and not page_short and "page" not in fields and "board" in fields:
                # No page: perhaps the observer screen, which names the ballot ("Ballot 41 of 316") above its identification line.
                screen = _screen_from_text(finder, prepared, work, extract_short, fields, counties)
                if screen is not None:
                    return _check_screen(workspace, log, finder, result, photo, screen, orientation, work, counties, scope, prepared=prepared)
            if ident is not None and "provenance" in ident.text.lower() and extract_short:
                found = finder.extracts_by_short(extract_short, counties)
                if len(found) == 1:
                    return _check_provenance(workspace, log, finder, result, found[0].sha256, prepared, work, counties, scope, by_qr=False)
            if page_short:
                candidates = finder.by_short(page_short, extract_short or None, counties)
                if len(candidates) == 1:
                    location = candidates[0]
                    result.located_by = "short_hashes"
                elif candidates:
                    result.reasons.append(f"the short hashes match {len(candidates)} pages: the QR code is needed to tell them apart")
            if location is None and ident is not None and not result.reasons:
                location, how = finder.by_ident(ident.text, counties)
                if location is not None:
                    result.located_by = "short_hashes"
                    result.notes.append(f"located by {how}")
                    page_short = location.sha256[:8]
                else:
                    result.reasons.append(f"no QR code could be read, and {how}")
            if location is None:
                if not result.reasons:
                    result.reasons.append(
                        "no QR code could be read, and no page of " + scope + " has the short hashes read from the identification line"
                        if page_short
                        else "neither the QR code nor the identification line could be read"
                    )
                _verdict(result)
                return result
        result.location = location
        result.kind = "page"
        result.extract_sha256 = location.extract.sha256
        result.county = location.extract.county
        result.board = location.board
        if not location.extract.latest:
            result.notes.append("this page is from an extract that has since been superseded: it is not the county's latest")
        if location.extract.purpose == "practice":
            result.notes.append("this page is from a practice extract")
        wanted = {"page index": location.extract.page_index_sha256 or "", "extract": location.extract.sha256}
        result.commitments = commitments(workspace, log, {k: v for k, v in wanted.items() if v})
        extract = finder.extract(location.extract.sha256)
        page = committed_page(finder, location)
        if extract is not None and page is None and location.layout not in layouts(extract):
            result.reasons.append(
                f"the page is in the index, but in a layout ({location.layout}) of an earlier page format that this version no longer draws or reproduces: "
                "build the extract again and print it from the PDF"
            )
            result.location = None
            _verdict(result)
            result.summary = f"{Verdict.UNKNOWN}: {location.label}"
            return result
        if extract is None or page is None:
            result.reasons.append("the page is in the index, but its extract is missing from the workspace or does not reproduce it")
            result.location = None
            _verdict(result)
            result.summary = f"{Verdict.UNKNOWN}: {location.label}"
            return result
        if _columns_of(location.layout) > 1 and result.located_by != "qr":
            # Located from the text: straighten each column on its own now that the layout is known.
            prepared = prepare(photo.image, work, orientation, columns=_columns_of(location.layout))
        expected = expected_lines(extract, location, page)
        all_ids = {str(b["imprinted_id"]): int(b["board_position"]) for b in extract["ballots"] if int(b["board"]) == location.board}
        reader = _Reader(prepared, work, _columns_of(location.layout))
        reader.run()
        result.lines = _compare_page(reader, expected, all_ids)
        crop_lines(result, prepared.image)
    _verdict(result)
    return result


#: A line's crop is made this many pixels high, and at most this wide.
CROP_HEIGHT = 56
CROP_WIDTH = 1400
#: At most this many lines are cut out.
MAX_CROPS = 60


def crop_lines(result: CheckResult, image: Image.Image) -> None:
    """Cut out each line not matched that was found (`LineResult.box`), a little padded, as a small grey PNG in ``result.crops``."""
    for line in result.lines:
        if line.status == "matched" or line.box is None or len(result.crops) >= MAX_CROPS:
            continue
        left, top, right, bottom = line.box
        pad = max(4, (bottom - top) // 3)
        box = (max(0, left - pad), max(0, top - pad), min(image.width, right + pad), min(image.height, bottom + pad))
        if box[2] - box[0] < 2 or box[3] - box[1] < 2:
            continue
        crop = grey(image.crop(box))
        factor = min(CROP_HEIGHT / crop.height, CROP_WIDTH / crop.width)
        crop = crop.resize((max(1, round(crop.width * factor)), max(1, round(crop.height * factor))), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        crop.save(out, "PNG", optimize=True)
        result.crops[line.key] = out.getvalue()


def _find_ident_line(image: Image.Image, work: Path) -> Line | None:
    """The identification line, read from the whole page, then from the page smoothed (a photo of a screen, or a grainy one), then from its top fifth alone."""
    attempts: list[tuple[str, Callable[[], Image.Image], int]] = [
        ("find", lambda: image, 6),
        ("find-smooth", lambda: flatten(image).filter(ImageFilter.GaussianBlur(1.0)), 6),
        ("find-top", lambda: flatten(image.crop((0, 0, image.width, max(1, image.height // 5)))).filter(ImageFilter.GaussianBlur(1.0)), 4),
    ]
    for name, make, psm in attempts:
        ident = find_ident(ocr(make(), work, psm=psm, name=name))
        if ident is not None:
            return ident
    return None


def _check_provenance(
    workspace: Workspace,
    log: EventLog,
    finder: PageFinder,
    result: CheckResult,
    extract_sha: str,
    prepared: Prepared,
    work: Path,
    counties: Sequence[str] | None,
    scope: str,
    *,
    by_qr: bool,
) -> CheckResult:
    result.kind = "provenance"
    result.located_by = "qr" if by_qr else "short_hashes"
    record = finder.extract_record(extract_sha, counties)
    extract = finder.extract(extract_sha) if record else None
    if record is None or extract is None:
        result.reasons.append(f"the extract hash {extract_sha[:8]}… is in none of {scope}")
        result.kind = None
        _verdict(result)
        return result
    result.extract_sha256 = record.sha256
    result.county = record.county
    wanted = {"extract": record.sha256}
    if record.page_index_sha256:
        wanted["page index"] = record.page_index_sha256
    result.commitments = commitments(workspace, log, wanted)
    boards = sorted(int(b) for b in extract["boards"])
    expected = expected_provenance(extract, record.sha256, boards[0])
    passes = [("pass 1", ocr(prepared.image, work, psm=4, name="prov1")), ("pass 2", ocr(flatten(prepared.image), work, psm=4, name="prov2"))]
    head = find_ident(passes[0][1])
    if head:
        m = re.search(r"board\W{0,2}(\d{1,2})", head.text, re.IGNORECASE)
        if m and int(m.group(1)) in boards:
            result.board = int(m.group(1))
    readings: dict[str, list[Reading]] = {e.key: [] for e in expected}
    boxes: dict[str, tuple[int, int, int, int]] = {}
    found_lines: dict[str, Line] = {}
    for name, lines in passes:
        for e in expected:
            line = _fact_line(e.label, lines)
            if line is None:
                continue
            boxes.setdefault(e.key, line.box)
            found_lines.setdefault(e.key, line)
            readings[e.key].append(_fact_reading(e, line, name))
    for e in expected:
        if _judge_votes(_fact_value(e.printed_value), readings[e.key])[0] == "matched" or e.key not in boxes:
            continue
        if re.fullmatch(r"[0-9a-f]{64}", e.printed_value) and e.key in found_lines:
            # A hash, group by group, each read again as hexadecimal.
            groups = [w for w in found_lines[e.key].words[len(e.label.split()) :] if 6 <= len(w.text) <= 10]
            for binary in (False, True):
                image = binarize(prepared.image) if binary else prepared.image
                parts = []
                for w in groups:
                    got = ocr_region(image, (w.left, w.top, w.right, w.bottom), work, whitelist="0123456789abcdef", factor=0.0, tight=True)
                    parts.append("".join(x.text for x in got.words))
                hexed = "".join(parts)
                vote = hexed if re.fullmatch(r"[0-9a-f]{64}", hexed) and not near_miss(hexed, e.printed_value, "hash") else None
                readings[e.key].append(Reading(vote, " ".join(parts), "re-read", weight=1.5))
        else:
            for binary in (False, True):
                line = ocr_region(binarize(prepared.image) if binary else prepared.image, boxes[e.key], work, factor=0.0)
                if line.words:
                    readings[e.key].append(_fact_reading(e, line, "re-read"))
    for e in expected:
        status, other = _judge_votes(_fact_value(e.printed_value), readings[e.key])
        texts = [r.text for r in readings[e.key] if r.text]
        read = other if status != "matched" else None
        line_result = LineResult(e.key, "fact", e.text, status, read or (texts[0] if status == "unread" and texts else None), "" if readings[e.key] else "not found on the photo")
        if status != "matched":
            vote = _fact_value(e.printed_value)
            line_result.reason = why_not_matched(status, vote, readings[e.key])
            line_result.reads = [reading_record(r, vote) for r in readings[e.key]]
            line_result.box = boxes.get(e.key)
        result.lines.append(line_result)
    crop_lines(result, prepared.image)
    _verdict(result)
    return result


def _fact_value(value: str) -> str:
    return value.lower() if re.fullmatch(r"[0-9a-f]{64}", value) else norm(value)


def _fact_line(label: str, lines: Sequence[Line]) -> Line | None:
    target = norm(label)
    best, best_score = None, 0.0
    for line in lines:
        start = norm(line.text)[: len(target) + 1]
        score = SequenceMatcher(None, start[: len(target)], target).ratio()
        if score > best_score:
            best, best_score = line, score
    return best if best_score >= 0.85 else None


def _fact_reading(e: Expected, line: Line, source: str) -> Reading:
    words = [w.text for w in line.words]
    label_words = len(e.label.split())
    rest = words[label_words:]
    if re.fullmatch(r"[0-9a-f]{64}", e.printed_value):
        hexed = "".join(rest).translate(_HEX_LOOKALIKES).lower()
        hexed = re.sub(r"[^0-9a-z]", "", hexed)
        vote = hexed if re.fullmatch(r"[0-9a-f]{64}", hexed) and not near_miss(hexed, e.printed_value, "hash") else None
        return Reading(vote, " ".join(rest), source, line_box=line.box)
    text = norm(" ".join(rest))
    expected = norm(e.printed_value)
    vote = expected if text == expected else (("other:" + text) if text and line.conf >= 85 and similarity(text, expected) < 0.5 else None)
    return Reading(vote, " ".join(rest), source, line_box=line.box)


# ---------------------------------------------------------------------------
# Typed short hashes


@dataclass
class Lookup:
    """Pages found by typed short hashes, each with its commitments."""

    pages: list[tuple[PageLocation, list[Commitment]]]
    error: str | None = None


def lookup_short(workspace: Workspace, log: EventLog, extract_short: str, page_short: str, *, counties: Sequence[str] | None = None) -> Lookup:
    """The pages whose hashes start with the typed short hashes (8 hex digits each, as printed), with their commitments."""
    extract_short, page_short = extract_short.strip().lower(), page_short.strip().lower()
    if not _SHORT.match(page_short) or (extract_short and not _SHORT.match(extract_short)):
        return Lookup([], "type the hashes as printed: 8 hex digits (0 to 9 and a to f) each")
    finder = PageFinder(workspace, log)
    found = finder.by_short(page_short, extract_short or None, counties)
    return Lookup(
        [(loc, commitments(workspace, log, {k: v for k, v in {"page index": loc.extract.page_index_sha256 or "", "extract": loc.extract.sha256}.items() if v})) for loc in found]
    )


def page_text(finder: PageFinder, location: PageLocation) -> list[Expected]:
    """The committed page's lines as printed, for showing beside a photo."""
    extract = finder.extract(location.extract.sha256)
    page = committed_page(finder, location)
    return expected_lines(extract, location, page) if extract and page else []


# ---------------------------------------------------------------------------
# Recording


def record_check(
    workspace: Workspace,
    log: EventLog,
    result: CheckResult,
    photo: Photo | None,
    *,
    actor: str,
    via: str,
    store: bool = True,
    session_id: str | None = None,
) -> Event:
    """Store the photo (without metadata) under its hash, if ``store``, and record ``photo.checked`` (see the module documentation)."""
    check_actor(actor)
    if via not in VIA:
        raise ValueError(f"via must be one of {', '.join(VIA)}")
    stored = False
    if store and photo is not None:
        workspace.put_bytes(photo.clean)
        stored = True
    location = result.location
    differences = [
        {"kind": line.kind, "n": line.n, "contest": line.contest} for line in result.differences()[:MAX_EVENT_DIFFERENCES]
    ]
    return log.append(
        "photo.checked",
        actor,
        county=result.county,
        board=str(result.board) if result.board is not None else None,
        session_id=session_id,
        file_sha256=result.photo_sha256 if stored else None,
        data={
            "via": via,
            "photo_sha256": result.photo_sha256,
            "stored": stored,
            "size": result.size,
            "media_type": result.media_type,
            "received_size": photo.original_size if photo is not None else None,
            "received_dimensions": list(photo.received_dimensions) if photo is not None else None,
            "stored_reduced": photo.stored_reduced if photo is not None else False,
            "kind": result.kind,
            "located_by": result.located_by,
            "page_sha256": location.sha256 if location else None,
            "extract_sha256": result.extract_sha256,
            "layout": location.layout if location else None,
            "page": location.page if location else None,
            "pages": location.pages if location else None,
            "ballot": result.screen.n if result.screen else None,
            "verdict": result.verdict,
            "content_verdict": result.verdict,
            "commitment_status": result.commitment_status,
            "committed": result.committed,
            "commitment": _commitment_record(result.commitments[0]) if result.commitments else None,
            "counts": result.counts(),
            "differences": differences,
            "software": running_software(),
        },
    )


def _commitment_record(c: Commitment) -> dict[str, Any]:
    return {
        "version": c.version,
        "time": c.time,
        "sums_sha256": c.sums_sha256,
        "bitcoin_block": c.bitcoin_block,
        "rekor_log_index": c.rekor_log_index,
        "commit": c.commit,
    }


def photo_checked_by(log: EventLog, sha: str, actor: str | None = None) -> Event | None:
    """The latest ``photo.checked`` event that stored the photo with this hash (by ``actor``, if given)."""
    found = None
    for event in log.iter_events():
        if event.kind == "photo.checked" and event.file_sha256 == sha and (actor is None or event.actor == actor):
            found = event
    return found



# ---------------------------------------------------------------------------
# The observer screen


_BALLOT_POS = re.compile(r"ballot\W{0,2}([0-9oOlI]{1,5})\W{0,2}of\W{0,2}([0-9oOlI]{1,5})", re.IGNORECASE)
#: What the screen's status cells and buttons are read as (contest by contest: the readings' numbers and outcomes, the pencil and "?"), dropped before a row is judged.
_SCREEN_JUNK = re.compile(r"^[^0-9A-Za-z]*$|^.$|^(?:1st|2nd)$")


def _screen_from_text(
    finder: PageFinder, prepared: Prepared, work: Path, extract_short: str, fields: Mapping[str, str], counties: Sequence[str] | None
) -> tuple[str, int, int] | None:
    """The ballot an observer screen shows, from its text alone (no QR code read): the extract's short hash, the board and "Ballot n of N"."""
    board = re.search(r"\d+", fields.get("board", ""))
    if board is None:
        return None
    for line in ocr(prepared.image, work, psm=6, name="screen-find"):
        m = _BALLOT_POS.search(line.text)
        if m:
            n = int(m.group(1).translate(_DIGIT_LOOKALIKES))
            found = [r for r in finder.extracts_by_short(extract_short, counties)]
            if len(found) == 1 and finder.screen(found[0].sha256[:32], int(board.group()), n, counties) is not None:
                return (found[0].sha256[:32], int(board.group()), n)
    return None


def _screen_ident(lines: Sequence[Line]) -> Line | None:
    """The observer screen's identification line ("Weld · Board 1 · extract acbe1a4f"), joined with the line before it if it wrapped
    before "extract" (on a phone)."""
    for i, line in enumerate(lines):
        low = line.text.lower()
        if "extract" not in low:
            continue
        if "board" in low:
            return line
        if i > 0 and "board" in lines[i - 1].text.lower():
            return Line([*lines[i - 1].words, *line.words])
    return find_ident(lines)


def expected_screen(extract: Mapping[str, Any], location: ScreenLocation) -> list[Expected]:
    """What the observer screen shows for one ballot (`corla_observer.web.observe`): its header (imprinted ID, "Ballot n of N", the
    extract's identification), then one line per contest, with the screen's names (`corla_observer.printnames`, layout "screen") and
    the contest's choices together (they may wrap on the screen)."""
    ballot = next(b for b in extract["ballots"] if int(b["board"]) == location.board and int(b["board_position"]) == location.n)
    names = print_names(extract, "screen")
    choices: dict[str, list[str]] = {str(c): [str(x) for x in v] for c, v in (extract.get("choices") or {}).items()}
    out = [
        Expected("screen:id", "id", location.imprinted_id, position=int(ballot["position"]), n=location.n, imprinted_id=location.imprinted_id),
        Expected("screen:place", "ident", f"Ballot {location.n} of {location.total}", label="place"),
        Expected("ident:county", "ident", str(extract["county"]), label="county"),
        Expected("ident:board", "ident", f"Board {location.board}", label="board"),
        Expected("ident:extract", "ident", f"extract {location.extract.sha256[:8]}", label="extract"),
    ]
    if ballot["status"] != "votes":
        text = "Redacted: this ballot's votes are not in the redacted CVR" if ballot["status"] == "redacted" else "Not in the CVR"
        out.append(Expected("screen:status", "marks", text, n=location.n, imprinted_id=location.imprinted_id))
        return out
    for k, (contest, value) in enumerate(ballot["votes"]):
        shown = " ".join(names.vote_lines(str(value)))
        candidates = [shown] + [names.vote_lines(c)[0] for c in choices.get(str(contest), [])] + ["NO VOTE"]
        printed_contest = names.contest(str(contest))
        out.append(
            Expected(
                f"screen:c{k}", "row", f"{printed_contest} | {shown}", 0, int(ballot["position"]), location.n, location.imprinted_id,
                str(contest), printed_contest, shown, tuple(dict.fromkeys(candidates)),
            )
        )  # fmt: skip
    return out


def _screen_split(lines: Sequence[Sequence[Any]], width: int) -> float:
    """Where the screen's contest names end and its choices begin: the x inside the most lines' spaces between words (the channel
    down the middle, where the contest names, right-aligned, meet the choices, left-aligned)."""
    spans: list[tuple[float, float]] = []
    for words in lines:
        ordered = sorted(words, key=lambda w: w.left)
        spans.extend((a.right, b.left) for a, b in zip(ordered, ordered[1:]) if b.left - a.right > 0.012 * width)
    if not spans:
        return width / 2
    candidates = sorted({round((x0 + x1) / 2) for x0, x1 in spans})
    best = max(candidates, key=lambda c: (sum(1 for x0, x1 in spans if x0 <= c <= x1), -abs(c - width / 2)))
    return float(best)


def _screen_rows(lines: Sequence[OcrLine], width: int) -> list[OcrLine]:
    """The screen's rows as read: the status cells' and buttons' words dropped, and the lines of one row (a contest's name or
    choices wrapped onto further lines) joined, the name's words first, then the choices', by which side of the middle they
    are on. Lines belong to one row when the space between them is less than most of a line: rows are further apart."""
    import statistics as stats

    cleaned = [[w for w in o.line.words if not _SCREEN_JUNK.match(w.text)] for o in lines]
    cleaned = [sorted(words, key=lambda w: w.left) for words in cleaned if words]
    if not cleaned:
        return []
    split = _screen_split(cleaned, width)
    height = stats.median(max(w.bottom for w in ws) - min(w.top for w in ws) for ws in cleaned)
    groups: list[list[list[Any]]] = []
    bottom = None
    for words in cleaned:
        top = min(w.top for w in words)
        if groups and bottom is not None and top - bottom < 0.8 * height:
            groups[-1].append(words)
        else:
            groups.append([words])
        bottom = max(w.bottom for w in words)
    out = []
    for group in groups:
        left = [w for ws in group for w in ws if (w.left + w.right) / 2 < split]
        right = [w for ws in group for w in ws if (w.left + w.right) / 2 >= split]
        line = Line(left + right)
        out.append(OcrLine(line, "row", [w.text for w in line.words]))
    return out


def _check_screen(
    workspace: Workspace,
    log: EventLog,
    finder: PageFinder,
    result: CheckResult,
    photo: Photo,
    found: tuple[str, int, int],
    orientation: str | None,
    work: Path,
    counties: Sequence[str] | None,
    scope: str,
    *,
    prepared: Prepared | None = None,
) -> CheckResult:
    """Check a screenshot or photo of the observer screen showing one ballot: its header and every contest shown (see docs/PHOTO_CHECK.md)."""
    result.located_by = "qr" if prepared is None else "short_hashes"
    location = finder.screen(found[0], found[1], found[2], counties)
    if location is None:
        result.reasons.append(f"the observer screen's ballot (extract {found[0][:8]}…, board {found[1]}, ballot {found[2]}) is in none of {scope}")
        _verdict(result)
        return result
    result.kind = "screen"
    result.screen = location
    result.extract_sha256 = location.extract.sha256
    result.county = location.extract.county
    result.board = location.board
    if not location.extract.latest:
        result.notes.append("this ballot is from an extract that has since been superseded: it is not the county's latest")
    if location.extract.purpose == "practice":
        result.notes.append("this ballot is from a practice extract")
    wanted = {"page index": location.extract.page_index_sha256 or "", "extract": location.extract.sha256}
    result.commitments = commitments(workspace, log, {k: v for k, v in wanted.items() if v})
    extract = finder.extract(location.extract.sha256)
    assert extract is not None
    expected = expected_screen(extract, location)
    prepared = prepared or prepare(photo.image, work, orientation, columns=1)
    passes = [("pass 1", ocr(prepared.image, work, psm=6, name="screen1")), ("pass 2", ocr(flatten(prepared.image), work, psm=4, name="screen2"))]
    readings: dict[str, list[Reading]] = {e.key: [] for e in expected}
    rows = [e for e in expected if e.kind == "row"]
    contests = sorted({e.printed_contest for e in rows})
    paired: set[int] = set()
    width = prepared.image.width
    for name, lines in passes:
        ident = _screen_ident(lines)
        header = [ln for ln in lines if ident is None or ln.box[1] <= ident.box[1]]
        fields = ident_fields(ident.text) if ident is not None else {}
        for e in expected:
            if e.kind == "ident" and e.label != "place":
                value = fields.get(e.label)
                vote = None if value and e.label == "extract" and near_miss(value, e.text, "short") else value
                readings[e.key].append(Reading(vote, value or "", name, line_box=ident.box if ident else None))
            elif e.key == "screen:place":
                m = next((m for ln in header if (m := _BALLOT_POS.search(ln.text))), None)
                value = f"Ballot {m.group(1).translate(_DIGIT_LOOKALIKES)} of {m.group(2).translate(_DIGIT_LOOKALIKES)}" if m else None
                # A digit more or less ("11" for "1") is OCR's to make: counted neither way, like an imprinted ID's.
                vote = None if value and near_miss(value, e.text, "id") else value
                readings[e.key].append(Reading(vote, value or "", name))
            elif e.kind == "id":
                imprinted = e.imprinted_id or ""
                for ln in header:
                    for w in ln.words:
                        got_id = normal_id(w.text, imprinted)
                        if got_id and well_formed_id(got_id, imprinted):
                            vote = None if near_miss(got_id, imprinted, "id") or padded(got_id, imprinted) else got_id
                            readings[e.key].append(Reading(vote, got_id, name, line_box=ln.box))
                            break
                    else:
                        continue
                    break
        body = [classify(ln) for ln in lines if ident is None or ln.box[1] > ident.box[3]]
        if rows:
            got = _screen_rows(body, width)
            pairs = align(rows, got)
            for i, j in pairs.items():
                paired.add(i)
                readings[rows[i].key].append(_row_reading(rows[i], got[j], name, contests))
        else:
            whole = next(e for e in expected if e.kind == "marks")
            text = " ".join(" ".join(o.words) for o in body)
            readings[whole.key].append(Reading(whole.text if similarity(whole.text, text[: len(whole.text) + 10]) >= 0.75 else None, text, name))
    # The extract's short hash, if not yet settled, cut out of the identification line and read again as hexadecimal.
    hash_e = next(e for e in expected if e.key == "ident:extract")
    if _status(hash_e, readings[hash_e.key])[0] != "matched":
        for _, lines in passes:
            ident = _screen_ident(lines)
            words = ident.words if ident else []
            at = [i for i, w in enumerate(words[:-1]) if similarity(w.text, "extract") >= 0.75]
            if not at:
                continue
            word = words[at[-1] + 1]
            enlarged = min(6.0, max(1.5, 160 / max(1, word.bottom - word.top)))
            for binary, factor in ((False, 0.0), (True, 0.0), (True, enlarged)):
                hexed = ocr_region(binarize(prepared.image) if binary else prepared.image, (word.left, word.top, word.right, word.bottom), work,
                                   whitelist=HEX_WHITELIST, factor=factor, tight=True)  # fmt: skip
                text = "".join(w.text for w in hexed.words).lower() if hexed else ""
                value = f"extract {text}" if _HEX8.match(text) and not near_miss(f"extract {text}", hash_e.text, "short") else None
                readings[hash_e.key].append(Reading(value, text, "re-read", weight=1.5))
            break
    # Rows outside the stretch read are not on this screenshot (a long ballot scrolls): not checked, and said so.
    shown = range(min(paired), max(paired) + 1) if paired else range(0)
    skipped = [e for i, e in enumerate(rows) if i not in shown]
    if rows and skipped:
        result.notes.append(
            f"contests {shown.start + 1} to {shown.stop} of {len(rows)} are on this screenshot and were checked; the others are not shown"
            if paired else "no contest of this ballot could be read on this screenshot"
        )
    for e in expected:
        if e in skipped:
            continue
        status, other = _status(e, readings[e.key])
        read: str | None = None
        if status != "matched":
            read = other[6:] if other and other.startswith("other:") else other
            if read is None:
                texts = [r.text for r in readings[e.key] if r.text]
                read = texts[0] if texts else None
        line_result = LineResult(e.key, e.kind, e.text, status, read, "" if readings[e.key] else "not found on the photo", n=e.n,
                                 imprinted_id=e.imprinted_id, contest=e.contest)  # fmt: skip
        if status != "matched":
            vote = _expected_vote(e)
            line_result.reason = why_not_matched(status, vote, readings[e.key])
            line_result.reads = [reading_record(r, vote) for r in readings[e.key]]
            line_result.box = next((r.line_box or r.value_box for r in readings[e.key] if r.line_box or r.value_box), None)
        result.lines.append(line_result)
    _distrust_scattered_rows(result.lines)
    crop_lines(result, prepared.image)
    _verdict(result)
    return result
