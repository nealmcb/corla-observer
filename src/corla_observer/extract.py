"""The observers' extract: each county's selected ballots in pull order, with their expected votes.

The extract is the reference observers follow (docs/PLAN.md, component 8),
on paper, in a spreadsheet or on the live observation page, all built from
the same structure. Its format, canonical serialization, hash and page
hashes are specified in docs/EXTRACT.md, so anyone can recompute them from
the committed inputs.

**Building.** `build_extract` takes, for one county:

- the county's pull list (CORLA's "List of ballots to audit", uploaded by
  its coordinator), which gives the ballots in pull order and their audit
  boards; or, until it arrives, the predicted selections
  (`corla_observer.predict`) in pull order (tabulator, batch, position, as
  CORLA's list sorts them), split into contiguous runs for the county's
  audit boards, and flagged as predicted;
- the county's redacted CVR, for each ballot's expected votes
  (`expected_votes`): a choice, several choices joined by `` + ``, an
  overvote (``overvote: A + B``), ``no vote``, or ``redacted`` per contest,
  and the whole ballot shown as redacted when its row has no votes left, or
  as not in the CVR;
- the data map's chosen manifest for the county (storage locations) and the
  seed (`corla_observer.seeds`);
- the latest prediction, for the contests each ballot was drawn for: the
  county's own contests and, alongside them, statewide (and multi-county)
  contests.

The result is a plain JSON structure (`EXTRACT_FORMAT`), stored in the
workspace under the SHA-256 of its canonical serialization
(`corla_observer.events.canonical_json`), with a page index (each page's
hash, `page_hashes`), and recorded in an ``extract.built`` event with the
hashes of every input, so it can be recomputed and checked.

**Pages.** `paginate` splits each board's ballots into printed pages by a
fixed rule for each layout (`LAYOUTS`, `block_lines`): a letter sheet,
portrait or landscape, with one, two or three columns of ballots
(`corla_observer.printlayout`), so each physical sheet is one page. Each page's hash is
the SHA-256 of the canonical serialization of its `page_record`, which
names its layout and commits to the extract's hash. A page is identified
by one short line (`page_line`: county, board, page N of M and the layout, and the
extract's and page's hashes, short), and its QR code carries the page's
hash (`qr_payload`). Each board's print starts with a provenance page
(`provenance`), not counted among its pages, listing the seed, the
extract's hash, every input's hash and the page index's hash in full, and
the key to the abbreviated names (`corla_observer.printnames`).

**Forms.** The printout is a PDF (`corla_observer.pdfextract`), byte for
byte reproducible; `render_text` (plain text), `render_provenance_html`
(the provenance page on screen) and `to_csv` (a spreadsheet, escaped
against formula injection) render the same structure. The printouts are compact, in
the style of the observers' printouts from Lori Stevens's RLAAuditHelper
(https://github.com/loriinboulder/AuditVerificationAssistant): per ballot,
one line with its imprinted ID centred in dashes, then one line per
contest, the contest's name right-aligned, a narrow column for the
observer's check mark, and the choices; names and choices are shortened to
fit the layout's columns and never wrap (`corla_observer.printnames`).

Events:

``extract.built``
    ``county`` set and ``file_sha256`` the extract's; ``data``: ``sha256``,
    ``size``, ``format``, ``purpose`` (``audit`` or ``practice``), ``order``
    (``pull_list`` or ``predicted``), ``inputs`` (as in the extract),
    ``ballots``, ``boards`` (ballots per board), ``redacted``,
    ``not_in_cvr``, ``pages`` (per layout, the pages per board),
    ``page_index_sha256``, ``pdfs`` (per layout, each board's printout's
    SHA-256, `corla_observer.pdfextract`), ``software`` (the version that
    built it). A targeted extract's (order ``targeted``) also has ``list``,
    the targeted list's key (`targeted_key`), and ``list_name``, its name.

**Pull lists.** A county can have, side by side, its RLA extract (the
latest extract whose order is not ``targeted``: the RLA sample) and any
number of targeted extracts, one per targeted list name
(`corla_observer.pulllists`). `latest_extract` and `latest_extract_events`
give the RLA extract only: building a targeted extract never changes what a
county's links show by default.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import textwrap
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corla_observer import printlayout
from corla_observer.classify import classify_bytes, is_test_county, known_county
from corla_observer.cvr import CvrReader, is_redaction_marker, unwrap
from corla_observer.datamap import DataMap, build_map, role_column, table_rows
from corla_observer.events import EventLog, JSONValue, canonical_json, check_actor
from corla_observer.predict import latest_prediction, published_selections
from corla_observer.printnames import PrintNames, base_contest, split_value
from corla_observer.pullorder import possible_boards, pull_order
from corla_observer.uploads import status as upload_status
from corla_observer.workspace import Workspace

EXTRACT_FORMAT = 2
PAGE_FORMAT = 6


def page_format(extract: Mapping[str, Any]) -> int:
    """The page format of an extract's pages: `PAGE_FORMAT`, or 2 for a version 1 extract (built before 25 October 2026), so its committed page hashes still check.

    (A version 2 extract's pages were page format 4 until the printout became a PDF, 26 October 2026,
    and page format 5 until a contest's several choices took a line each, 27 October 2026.)
    """
    return PAGE_FORMAT if int(extract.get("format", 1)) >= 2 else 2


MAX_BOARDS = 20
PURPOSES = ("audit", "practice")
#: The print layouts, each with the columns of ballots on a page and the
#: lines each column holds, as the PDF's geometry gives them
#: (`corla_observer.printlayout`): a letter sheet, portrait or landscape,
#: with one, two or three columns. Each sheet is one page.
LAYOUTS: dict[str, tuple[int, int]] = {name: (lay.columns, lay.column_lines) for name, lay in printlayout.LAYOUTS.items()}
#: The layout printed when none is chosen: portrait, two columns.
DEFAULT_LAYOUT = "portrait-2"
#: The layout of the plain text's pages: one column.
TEXT_LAYOUT = "portrait-1"
#: Lines a one-column portrait page holds, for `paginate`.
PAGE_LINES = LAYOUTS[TEXT_LAYOUT][1]
#: Lines per page of a version 1 extract's pages (page format 2), portrait only.
PAGE_LINES_FORMAT_2 = 70
#: A ballot's status.
STATUSES = ("votes", "redacted", "not_in_cvr")
#: The prefix of a page's QR code, and of the provenance page's (the extract's hash).
QR_PREFIX = "COX2"
QR_EXTRACT_PREFIX = "COX2E"
#: The prefix of the observer screen's QR code for one ballot (`screen_qr_payload`).
QR_SCREEN_PREFIX = "COX2S"
#: Hex digits of the extract's hash in a screen QR code.
SCREEN_QR_HASH = 32

KEEP_OUT_OF_SIGHT = (
    "KEEP OUT OF SIGHT of the audit board and the public: the audit board must not see the expected votes."
)
PREDICTED_NOTE = (
    "PREDICTED ORDER: the predicted selections in CORLA's pull order (storage location, scanner, batch, record), "
    "split among the audit boards as CORLA splits them; ballots at a board's ends may be the next or previous "
    "board's (marked BOUNDARY), and ballots marked POSSIBLE are drawn only if the sample count is above its low "
    "estimate. This is not the county's pull list, which has not been uploaded yet."
)
AUDIT_CENTER_NOTE = (
    "AUDIT CENTER ORDER: CORLA's published selections for the county, in CORLA's pull order (storage location, "
    "scanner, batch, record), split among the audit boards as CORLA splits them; ballots at a board's ends may be "
    "the next or previous board's (marked BOUNDARY). This is not the county's pull list, which has not been uploaded yet."
)
PULL_LIST_NOTE = "The county's pull list order and audit board assignment."
TARGETED_NOTE = (
    "TARGETED BALLOTS, NOT PART OF THE RLA SAMPLE: ballots the observers chose (corla_observer.targets), in CORLA's pull "
    "order (storage location, scanner, batch, record). They are examined after the state's sample and not entered into CORLA."
)
ORDER_NOTES = {"pull_list": PULL_LIST_NOTE, "audit_center": AUDIT_CENTER_NOTE, "predicted": PREDICTED_NOTE, "targeted": TARGETED_NOTE}
#: The order notices shown on the provenance page and in the spreadsheet (none for a pull list).
INTERIM_ORDERS = ("predicted", "audit_center", "targeted")

_VOTE_FOR_RE = re.compile(r"\s*\(Vote For=(\d+)\)\s*$", re.IGNORECASE)
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")


class ExtractError(ValueError):
    """An extract can't be built or shown as asked."""


#: The name of a targeted list built without one.
DEFAULT_TARGETED_NAME = "Targeted list"
#: Longest name of a targeted list.
MAX_LIST_NAME = 80


def targeted_key(name: str) -> str:
    """A targeted list's key, from its name: ``t-`` and the first 12 hex digits of the SHA-256 of the name (UTF-8)."""
    return "t-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def clean_list_name(name: str | None) -> str:
    """A targeted list's name with its spaces collapsed, at most `MAX_LIST_NAME` characters, else `DEFAULT_TARGETED_NAME`."""
    text = "".join(ch for ch in " ".join(str(name or "").split()) if ch.isprintable())[:MAX_LIST_NAME].strip()
    return text or DEFAULT_TARGETED_NAME


def is_targeted_event(event: Any) -> bool:
    """Whether an ``extract.built`` event is of a targeted extract."""
    return bool(event.data.get("order") == "targeted")


# ---------------------------------------------------------------------------
# Expected votes


@dataclass
class ExpectedBallot:
    imprinted_id: str
    #: ``(contest, what was marked)`` for each contest on the card: a choice,
    #: several choices joined by " + ", "overvote: A + B", "no vote", or "redacted".
    votes: list[tuple[str, str]] = field(default_factory=list)
    #: True if the row has no vote values left.
    redacted: bool = False


def contest_label(name: str) -> tuple[str, int]:
    """A contest name without its ``(Vote For=N)``, and N (1 if absent)."""
    match = _VOTE_FOR_RE.search(name)
    if match:
        return name[: match.start()].strip(), int(match.group(1))
    return name.strip(), 1


def id_key(imprinted_id: str) -> str:
    """An imprinted ID compared without spaces or leading zeros in its parts."""
    return "-".join(part.strip().lstrip("0") or "0" for part in imprinted_id.split("-"))


def pull_order_key(imprinted_id: str) -> tuple[Any, ...]:
    """CORLA's pull-list sort: tabulator, batch and position, numerically where they are numbers."""
    parts = imprinted_id.split("-")
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)


def expected_votes(path: Path | str, wanted: Iterable[str]) -> tuple[dict[str, ExpectedBallot], dict[str, list[str]]]:
    """Each wanted ballot's expected votes from a Dominion CVR export, and every contest's choices.

    Ballots are matched by imprinted ID, or by tabulator, batch and record
    when the imprinted ID column doesn't match. More choices marked than a
    contest's ``Vote For`` is an overvote. Returns ``(ballots by imprinted
    ID as given, choices by contest label)``.
    """
    reader = CvrReader(path)
    h = reader.header
    keys = {id_key(w): w for w in wanted}
    with open(reader.path, encoding=h.encoding, newline="") as f:
        rows = csv.reader(f, delimiter=h.delimiter)
        head: list[list[str]] = []
        for number, row in enumerate(rows, start=1):
            head.append(row)
            if number >= h.header_row:
                break
        choice_row = head[h.header_row - 2] if h.header_row >= 2 else []
        labels = [contest_label(c) for c in h.contests]
        columns: list[list[tuple[int, str]]] = [[] for _ in h.contests]
        for offset, contest in enumerate(h.column_contest):
            index = h.vote_start + offset
            choice = unwrap(choice_row[index]) if index < len(choice_row) else ""
            columns[contest].append((index, choice or f"choice {len(columns[contest]) + 1}"))
        choices = {labels[i][0]: [c for _, c in cols] for i, cols in enumerate(columns)}
        found: dict[str, ExpectedBallot] = {}
        for row in rows:
            if not any(cell.strip() for cell in row):
                continue
            key = None
            imprinted = unwrap(row[h.imprinted_index]) if h.imprinted_index < len(row) else ""
            if imprinted and id_key(imprinted) in keys:
                key = keys[id_key(imprinted)]
            elif None not in (h.tabulator_index, h.batch_index, h.record_index):
                assert h.tabulator_index is not None and h.batch_index is not None and h.record_index is not None
                parts = [unwrap(row[i]) if i < len(row) else "" for i in (h.tabulator_index, h.batch_index, h.record_index)]
                if all(parts) and id_key("-".join(parts)) in keys:
                    key = keys[id_key("-".join(parts))]
            if key is None or key in found:
                continue
            ballot = ExpectedBallot(imprinted_id=key)
            any_value = False
            for i, cols in enumerate(columns):
                cells = [unwrap(row[index]).strip() if index < len(row) else "" for index, _ in cols]
                if not any(cells):
                    continue  # not on this card
                if all(not c or is_redaction_marker(c) for c in cells):
                    ballot.votes.append((labels[i][0], "redacted"))
                    continue
                any_value = True
                marked = [choice for (_, choice), cell in zip(cols, cells, strict=True) if cell.isdigit() and int(cell) > 0]
                if not marked:
                    value = "no vote"
                elif len(marked) > labels[i][1]:
                    value = "overvote: " + " + ".join(marked)
                else:
                    value = " + ".join(marked)
                ballot.votes.append((labels[i][0], value))
            ballot.redacted = not any_value
            if ballot.redacted:
                ballot.votes = []
            found[key] = ballot
    return found, choices


# ---------------------------------------------------------------------------
# The pull list


@dataclass(frozen=True)
class PullRow:
    imprinted_id: str
    #: The audit board as the list gives it, or None if the list has no board column.
    board_label: str | None
    location: str | None


def read_pull_list(data: bytes) -> list[PullRow]:
    """A pull list's ballots, in its own order (each ballot once).

    Raises:
        ExtractError: if the file is not a pull list `corla_observer.classify` recognizes.
    """
    classification = classify_bytes(data)
    if classification.kind != "pull_list":
        raise ExtractError(f"the pull list is not recognized as one (classified as {classification.kind})")
    _, rows = table_rows(data, classification)
    columns = {r: role_column(classification, r) for r in ("imprinted_id", "tabulator", "batch", "record", "audit_board", "location")}
    result: list[PullRow] = []
    seen: set[str] = set()
    for row in rows:
        def cell(role: str) -> str:
            name = columns[role]
            return (row.get(name) or "").strip() if name else ""

        imprinted = cell("imprinted_id")
        if not imprinted and all(cell(r) for r in ("tabulator", "batch", "record")):
            imprinted = "-".join(cell(r) for r in ("tabulator", "batch", "record"))
        if not imprinted or id_key(imprinted) in seen:
            continue
        seen.add(id_key(imprinted))
        board = cell("audit_board") if columns["audit_board"] else None
        result.append(PullRow(imprinted, board, cell("location") or None))
    if not result:
        raise ExtractError("the pull list lists no ballots")
    return result


def board_numbers(labels: Sequence[str]) -> dict[str, int]:
    """Audit board numbers for a pull list's board labels.

    Each label's last number ("Audit Board 2" is 2) when every label has
    one and they are distinct and 1 to `MAX_BOARDS`; otherwise the order in
    which the labels first appear.
    """
    distinct = list(dict.fromkeys(labels))
    numbers = {}
    for label in distinct:
        digits = re.findall(r"\d+", label)
        numbers[label] = int(digits[-1]) if digits else 0
    values = list(numbers.values())
    if len(set(values)) != len(values) or any(not 1 <= v <= MAX_BOARDS for v in values):
        return {label: n for n, label in enumerate(distinct, start=1)}
    return numbers


# ---------------------------------------------------------------------------
# Building


def _scope(contest: Mapping[str, Any]) -> str:
    if contest.get("statewide"):
        return "statewide"
    counties = contest.get("counties")
    return "multi-county" if isinstance(counties, list) and len(counties) > 1 else "county"


@dataclass
class _Picks:
    """Per imprinted ID drawn in a county (by `id_key`): its contests, and whether some draw of it is certain."""

    ids: dict[str, str] = field(default_factory=dict)
    names: dict[str, list[str]] = field(default_factory=dict)
    certain: set[str] = field(default_factory=set)
    contests: dict[str, dict[str, JSONValue]] = field(default_factory=dict)


def _picks(prediction: Mapping[str, Any], county: str) -> _Picks:
    """The prediction's draws in ``county``: each ballot's contests, and which ballots are certain.

    A draw is certain when its draw number is at most its contest's low
    count (``low_count``, which is the sample count once CORLA's is known):
    CORLA's draws are a fixed sequence, so it will be drawn if CORLA's count
    is at least the low estimate. A ballot is certain if any of its draws is.
    """
    found = _Picks()
    for contest in prediction.get("contests") or []:
        if not isinstance(contest, dict):
            continue
        name = str(contest.get("name"))
        low = contest.get("low_count", contest.get("sample_count"))
        low = low if isinstance(low, int) else contest.get("sample_count")
        for draw in contest.get("draws") or []:
            if not isinstance(draw, dict) or known_county(str(draw.get("county", ""))) != county:
                continue
            imprinted = str(draw["imprinted_id"])
            key = id_key(imprinted)
            found.ids.setdefault(key, imprinted)
            names = found.names.setdefault(key, [])
            if name not in names:
                names.append(name)
            index = draw.get("index")
            if isinstance(index, int) and isinstance(low, int) and index <= low:
                found.certain.add(key)
            counties = contest.get("counties")
            found.contests[name] = {"scope": _scope(contest), "counties": [str(c) for c in counties] if isinstance(counties, list) else []}
    return found


def _manifests_digest(datamap: DataMap) -> str | None:
    """SHA-256 of the chosen manifests' list: ``county TAB sha256 LF`` per county, sorted by county."""
    if not datamap.manifests:
        return None
    text = "".join(f"{c}\t{m.sha256}\n" for c, m in sorted(datamap.manifests.items()))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _locations(datamap: DataMap, county: str) -> dict[tuple[str, str], str]:
    chosen = datamap.manifests.get(county)
    if chosen is None:
        return {}
    return {(id_key(b.tabulator_id), id_key(b.batch_number)): b.location for b in chosen.batches if b.location}


def _location(imprinted: str, locations: Mapping[tuple[str, str], str]) -> str | None:
    parts = imprinted.split("-")
    return locations.get((id_key(parts[0]), id_key(parts[1]))) if len(parts) == 3 else None


def corla_order(ids: Iterable[str], locations: Mapping[tuple[str, str], str]) -> list[str]:
    """Imprinted IDs in CORLA's pull-list order (`corla_observer.pullorder`): location, scanner, batch, record."""

    def key(imprinted: str) -> tuple[str, str, str, str]:
        parts = imprinted.split("-")
        while len(parts) < 3:
            parts.append("")
        return (_location(imprinted, locations) or "", parts[0], parts[1], parts[2])

    return pull_order(list(ids), key)


def _split(n: int, boards: int) -> list[tuple[int, int]]:
    """``(board, board position)`` for ``n`` ballots split into ``boards`` contiguous runs (the practice reader's split)."""
    per_board = max(1, -(-n // boards))
    return [(i // per_board + 1, i % per_board + 1) for i in range(n)]


def boundary_note(other: Sequence[int]) -> str:
    """The hint on a ballot that may belong to another board: "BOUNDARY: may be Board 2's ballot. ..."."""
    boards = " or ".join(f"Board {b}" for b in other)
    return f"BOUNDARY: may be {boards}'s ballot. If this board doesn't pull it, skip it."


@dataclass
class BuiltExtract:
    extract: dict[str, Any]
    sha256: str
    seq: int
    page_index_sha256: str
    #: Each printout's PDF hash, by layout and board.
    pdfs: dict[str, dict[str, str]] = field(default_factory=dict)


def make_extract(
    *,
    county: str,
    election: str | None,
    seed: str | None,
    prediction: Mapping[str, Any] | None,
    cvr_path: Path,
    pull_list: bytes | None,
    boards: int,
    locations: Mapping[tuple[str, str], str],
    inputs: Mapping[str, JSONValue],
    purpose: str = "audit",
    published: Sequence[str] | None = None,
    first: str | None = None,
    targeted: bool = False,
    sources: Mapping[str, str] | None = None,
    in_rla: Collection[str] = (),
    targeted_in: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The extract structure from its inputs, without storing or recording anything (see docs/EXTRACT.md).

    ``published`` is CORLA's published selection of the county's ballots
    (the audit center's posting), if any. ``first`` says which of the pull
    list and the posting arrived first (``pull_list`` or ``audit_center``)
    when both are present: that one defines the ballots, and the other is
    an independent check. When the posting came first and the pull list
    lists exactly its ballots, the pull list's order and boards are used.

    With ``targeted``, ``pull_list`` is a targeted pull list
    (`corla_observer.targets`), not the county's, and the order is
    ``targeted``. ``sources`` tags a combined targeted list's ballots
    with the list each came from (by `id_key`), as ``source_list``.
    ``in_rla`` are the ballots (by `id_key`) the county's RLA sample also
    holds, marked ``in_rla`` on a targeted extract; ``targeted_in`` names,
    for an RLA extract, the earlier sent targeted list each ballot (by
    `id_key`) appeared on, marked ``targeted_in`` (docs/TARGETING.md,
    "Duplicates and overlaps"); both are summarized in ``checks``.

    ``prediction`` may be None when the pull list or the posting gives the
    ballots: the extract then names no contest a ballot was drawn for, and
    its ``checks`` have no ``not_predicted`` or ``predicted_not_listed``.
    """
    if purpose not in PURPOSES:
        raise ExtractError(f"purpose must be one of {', '.join(PURPOSES)}")
    picks = _picks(prediction, county) if prediction is not None else _Picks()
    contests = picks.contests
    rows = read_pull_list(pull_list) if pull_list is not None else None
    published_keys = {id_key(i): i for i in published} if published else {}
    if rows is not None and published_keys and first == "audit_center":
        if {id_key(r.imprinted_id) for r in rows} != set(published_keys):
            rows = None  # the posting defines the ballots; the pull list is the check
    # Each listed ballot: (imprinted ID, location given, possible boards).
    listed: list[tuple[str, str | None, tuple[int, ...]]]
    if rows is not None:
        order = "targeted" if targeted else "pull_list"
        if all(r.board_label is not None for r in rows):
            numbers = board_numbers([r.board_label or "" for r in rows])
            listed = [(r.imprinted_id, r.location, (numbers[r.board_label or ""],)) for r in rows]
            split = "pull_list"
        else:
            possible = possible_boards(len(rows), boards)
            listed = [(r.imprinted_id, r.location, possible[i]) for i, r in enumerate(rows)]
            split = "uncertain"
    else:
        if published_keys:
            order = "audit_center"
            ordered = corla_order(published_keys.values(), locations)
        else:
            if prediction is None:
                raise ExtractError(f"no pull list, no posting of CORLA's selections and no prediction for {county}")
            if not picks.ids:
                raise ExtractError(f"the prediction has no draws in {county}")
            order = "predicted"
            ordered = corla_order(picks.ids.values(), locations)
        possible = possible_boards(len(ordered), boards)
        listed = [(i, None, possible[n]) for n, i in enumerate(ordered)]
        split = "uncertain"
    found, choices = expected_votes(cvr_path, [i for i, _, _ in listed])
    ballots: list[dict[str, Any]] = []
    used: set[str] = set()
    counters: dict[int, int] = {}
    for position, (imprinted, listed_location, on_boards) in enumerate(listed, start=1):
        ballot = found.get(imprinted)
        votes = [[c, v] for c, v in ballot.votes] if ballot else []
        used.update(c for c, _ in votes)
        location = listed_location or _location(imprinted, locations)
        key = id_key(imprinted)
        drawn_for = picks.names.get(key, [])
        certain = order != "predicted" or key in picks.certain
        for board in on_boards:
            counters[board] = counters.get(board, 0) + 1
            ballots.append(
                {
                    "position": position,
                    "board": board,
                    "board_position": counters[board],
                    "imprinted_id": imprinted,
                    "location": location or None,
                    "status": "not_in_cvr" if ballot is None else "redacted" if ballot.redacted else "votes",
                    "votes": votes,
                    "picks": [[name, str(contests[name]["scope"])] for name in drawn_for],
                    "predicted": bool(drawn_for),
                    "certain": certain,
                    "boundary": [b for b in on_boards if b != board],
                }
                | ({"source_list": sources.get(key, "")} if sources else {})
                | ({"in_rla": True} if key in in_rla else {})
                | ({"targeted_in": targeted_in[key]} if targeted_in and key in targeted_in else {})
            )
    listed_keys = {id_key(i) for i, _, _ in listed}
    board_counts: dict[str, int] = {}
    for item in ballots:
        board_counts[str(item["board"])] = board_counts.get(str(item["board"]), 0) + 1
    other: dict[str, Any] | None = None
    if rows is not None and published_keys:
        other = {"source": "audit_center", "keys": set(published_keys), "ids": published_keys}
    elif order == "audit_center" and pull_list is not None:
        pull_rows = read_pull_list(pull_list)
        other = {"source": "pull_list", "keys": {id_key(r.imprinted_id) for r in pull_rows}, "ids": {id_key(r.imprinted_id): r.imprinted_id for r in pull_rows}}
    checks: dict[str, Any] = {}
    if prediction is not None:
        checks["not_predicted"] = [i for i, _, _ in listed if id_key(i) not in picks.names]
        checks["predicted_not_listed"] = sorted((i for k, i in picks.ids.items() if k not in listed_keys), key=pull_order_key)
    if other is not None:
        checks["other_source"] = {
            "source": other["source"],
            "not_listed": sorted((other["ids"][k] for k in other["keys"] - listed_keys), key=pull_order_key),
            "not_in_other": sorted((i for i, _, _ in listed if id_key(i) not in other["keys"]), key=pull_order_key),
        }
        checks["sources_disagree"] = bool(checks["other_source"]["not_listed"] or checks["other_source"]["not_in_other"])
    if in_rla:
        checks["in_rla"] = [i for i, _, _ in listed if id_key(i) in in_rla]
    if targeted_in:
        checks["previously_targeted"] = [{"imprinted_id": i, "list": targeted_in[id_key(i)]} for i, _, _ in listed if id_key(i) in targeted_in]
    return {
        "format": EXTRACT_FORMAT,
        "kind": "observer_extract",
        "purpose": purpose,
        "election": election,
        "county": county,
        "seed": seed,
        "order": order,
        "order_note": ORDER_NOTES[order],
        "board_split": split,
        "inputs": dict(inputs),
        "boards": dict(sorted(board_counts.items(), key=lambda kv: int(kv[0]))),
        "contests": {name: contests[name] for name in sorted(contests)},
        "choices": {c: choices[c] for c in sorted(used) if c in choices},
        "checks": checks,
        "ballots": ballots,
    }


def _latest_upload(log: EventLog, county: str, kind: str) -> str | None:
    entry = upload_status(log, [county]).get(county)
    received = entry.latest.get(kind) if entry else None
    return received.sha256 if received else None


@dataclass
class _Sources:
    """What a county's extract is built from, besides its uploads (`build_extract`)."""

    seed: str | None
    prediction: Mapping[str, Any] | None
    prediction_sha: str | None
    election: str | None
    locations: dict[tuple[str, str], str]
    county_manifest: str | None
    manifests: str | None
    overrides: str | None
    published: list[str] | None = None
    posted_files: dict[str, str | None] = field(default_factory=dict)
    first: str | None = None


def _real_sources(workspace: Workspace, log: EventLog, name: str, datamap: DataMap, pull_sha: str | None) -> _Sources:
    """A real county's: the data map's seed, manifests and overrides, CORLA's posting, and the prediction if one was made from the seed in effect."""
    seed = str(datamap.seed.value) if datamap.seed is not None else None
    prediction: Mapping[str, Any] | None = None
    prediction_sha = None
    latest = latest_prediction(workspace, log) if seed else None
    if latest is not None and latest[1].get("seed") == seed:
        prediction_seq, prediction = latest
        prediction_sha = next(
            (e.file_sha256 for e in log.iter_events() if e.kind == "prediction.made" and e.seq == prediction_seq), None
        )
    chosen = datamap.manifests.get(name)
    posting = published_selections(datamap, workspace)
    county_posting = posting.by_county.get(name) or {}
    published = list(dict.fromkeys(i for ids in county_posting.values() for i in ids)) or None
    posted = {e.ref: e.sha256 for e in datamap.entries if e.ref in posting.refs} if published else {}
    first = None
    if published and pull_sha:
        pull_entry = datamap.entry(f"upload:{name}:pull_list")
        pulled = pull_entry.obtained if pull_entry else None
        first = "audit_center" if posting.obtained and pulled and posting.obtained < pulled else "pull_list"
    if pull_sha is None and not published:
        if seed is None:
            raise ExtractError(f"no pull list from {name} and no seed: upload its pull list, or enter the seed and predict")
        if prediction is None:
            raise ExtractError(
                f"no pull list from {name}, no posting of CORLA's selections, and no prediction from the seed in effect: "
                "upload its pull list, or predict the selections first"
            )
    election = datamap.election or (str(prediction.get("election")) if prediction and prediction.get("election") else None)
    return _Sources(
        seed=seed,
        prediction=prediction,
        prediction_sha=prediction_sha,
        election=election,
        locations=_locations(datamap, name),
        county_manifest=chosen.sha256 if chosen else None,
        manifests=_manifests_digest(datamap),
        overrides=datamap.overrides_sha256,
        published=published,
        posted_files={"selection_list": posted.get(posting.refs[0]), "cvr_comparison": posted.get(posting.refs[1])} if published else {},
        first=first,
    )


def _test_sources(
    workspace: Workspace, log: EventLog, name: str, datamap: DataMap, pull_sha: str | None, sample_count: int | None = None
) -> _Sources:
    """A test county's (`corla_observer.testcounty`): its own uploaded manifest, and without a pull list its own prediction.

    The prediction is from the seed in effect, or else the test county's
    own (`corla_observer.testcounty.SEED`), its manifest, its targeted
    contest and a fixed sample count (``sample_count``, by default
    `corla_observer.testcounty.SAMPLE_COUNT`).

    Nothing of a real election's (its manifests, overrides, prediction or
    CORLA's posting) goes into a test county's extract.
    """
    from corla_observer import testcounty
    from corla_observer.datamap import load_manifest_bytes

    seed = str(datamap.seed.value) if datamap.seed is not None else None
    seed_source = "the seed in effect"
    manifest_sha = _latest_upload(log, name, "manifest")
    batches = []
    if manifest_sha is not None:
        try:
            batches = load_manifest_bytes(workspace.get_bytes(manifest_sha), name=f"{name} manifest")
        except (ValueError, csv.Error) as exc:
            raise ExtractError(f"{name}'s manifest does not load: {exc}") from None
    locations = {(id_key(b.tabulator_id), id_key(b.batch_number)): b.location for b in batches if b.location}
    prediction: dict[str, Any] | None = None
    prediction_sha = None
    if pull_sha is None:
        if not batches:
            raise ExtractError(f"no pull list or manifest from {name}: upload its pull list, or its manifest to predict its selections")
        if seed is None:
            seed, seed_source = testcounty.SEED, "the test county's own seed (no seed entered)"
        try:
            prediction = testcounty.prediction(seed, batches, name, sample_count or testcounty.SAMPLE_COUNT, seed_source)
        except ValueError as exc:
            raise ExtractError(str(exc)) from None
        prediction_sha = workspace.put_bytes(canonical_json(prediction)).sha256
    return _Sources(
        seed=seed,
        prediction=prediction,
        prediction_sha=prediction_sha,
        election=None,
        locations=locations,
        county_manifest=manifest_sha,
        manifests=None,
        overrides=None,
    )


@dataclass
class CountyManifest:
    """A county's manifest as the extract uses it: storage locations and ballot counts by (tabulator, batch) as `id_key` gives them."""

    locations: dict[tuple[str, str], str] = field(default_factory=dict)
    sizes: dict[tuple[str, str], int] = field(default_factory=dict)
    sha256: str | None = None


def county_manifest(workspace: Workspace, log: EventLog, county: str, datamap: DataMap | None = None) -> CountyManifest:
    """A county's manifest: a real county's from the data map's choice, a test county's its own latest upload, as its extract's are.

    Empty without a manifest (or with one that does not load).
    """
    if is_test_county(county):
        from corla_observer.datamap import load_manifest_bytes

        sha = _latest_upload(log, county, "manifest")
        if sha is None:
            return CountyManifest()
        try:
            batches = load_manifest_bytes(workspace.get_bytes(sha), name=f"{county} manifest")
        except (ValueError, csv.Error):
            return CountyManifest()
    else:
        datamap = datamap or build_map(log, workspace)
        chosen = datamap.manifests.get(county)
        if chosen is None:
            return CountyManifest()
        batches, sha = list(chosen.batches), chosen.sha256
    found = CountyManifest(sha256=sha)
    for b in batches:
        key = (id_key(b.tabulator_id), id_key(b.batch_number))
        if b.location:
            found.locations[key] = b.location
        found.sizes[key] = found.sizes.get(key, 0) + int(b.ballot_count)
    return found


def build_extract(
    workspace: Workspace,
    log: EventLog,
    county: str,
    *,
    boards: int | None = None,
    purpose: str = "audit",
    actor: str = "system",
    datamap: DataMap | None = None,
    skip_if_unchanged: bool = False,
    sample_count: int | None = None,
    targeted: bytes | None = None,
    list_name: str | None = None,
    source_lists: Mapping[str, str] | None = None,
    list_record: str | None = None,
) -> BuiltExtract:
    """Build, store and record a county's extract from the workspace; see the module documentation.

    The ballots come from the county's pull list, or CORLA's posted
    selections, or (without either) the latest prediction made from the
    seed in effect. No prediction is needed with a pull list or a posting:
    the extract is then built from it and the redacted CVR, and a
    prediction from the seed in effect, if there is one, only says which
    contests each ballot was drawn for and which listed ballots it did not
    predict. A test county's extract uses its own uploads only
    (`_test_sources`).

    With ``skip_if_unchanged``, an extract identical to the county's latest
    one of the same purpose (for a targeted list, the latest under its
    name, with the same list record) is not recorded again (its event's seq
    is then the earlier one's).

    ``boards`` splits a predicted order (or a pull list without a board
    column) among that many boards; by default, the county's recorded
    audit board count, else 1. ``sample_count`` is a test county's fixed
    sample count when its selections are predicted (no pull list).

    ``targeted`` is a targeted pull list (`corla_observer.targets.pull_list_bytes`):
    the extract is then of those ballots, in order ``targeted``, instead of
    the county's own, and names it in its inputs as ``targeted_list``.
    ``list_name`` is the targeted list's name (`clean_list_name`), recorded in
    the event: a targeted extract built under a name already used replaces
    that list's earlier one, and never the county's RLA extract.
    ``source_lists`` tags a combined list's ballots with the list each came
    from (`make_extract`'s ``sources``), and ``list_record`` is the hash of the stored
    list record (`corla_observer.targets.list_record`), named in the
    event as ``list_record_sha256``.

    The overlaps are marked both ways (docs/TARGETING.md): a targeted
    extract's ballots that the county's RLA extract holds are marked
    ``in_rla``, and an RLA extract's ballots that an earlier sent targeted
    list holds are marked ``targeted_in`` with the list's label; the event
    counts each (``in_rla``, ``previously_targeted``).

    Raises:
        ExtractError: without the county's redacted CVR; without a pull
            list, a posting or a prediction from the seed in effect; or for
            an unreadable pull list.
    """
    name = known_county(county)
    if name is None:
        raise ExtractError(f"not a Colorado county: {county!r}")
    test = is_test_county(name)
    if test:
        purpose = "practice"  # a test county's extract is never an audit's (corla_observer.classify.TEST_COUNTIES)
    check_actor(actor)
    datamap = datamap or build_map(log, workspace)
    cvr_sha = _latest_upload(log, name, "redacted_cvr")
    if cvr_sha is None:
        raise ExtractError(f"no redacted CVR from {name} in the workspace")
    pull_sha = _latest_upload(log, name, "pull_list")
    targeted_sha = workspace.put_bytes(targeted).sha256 if targeted is not None else None
    if targeted_sha is not None:
        pull_sha = None
    if boards is None:
        entry = upload_status(log, [name]).get(name)
        boards = entry.audit_boards if entry and entry.audit_boards else 1
    if not 1 <= boards <= MAX_BOARDS:
        raise ExtractError(f"boards must be 1 to {MAX_BOARDS}")
    listed_sha = targeted_sha or pull_sha
    sources = (
        _test_sources(workspace, log, name, datamap, listed_sha, sample_count) if test else _real_sources(workspace, log, name, datamap, listed_sha)
    )
    published = None if targeted_sha else sources.published
    inputs: dict[str, JSONValue] = {
        "pull_list": pull_sha,
        "redacted_cvr": cvr_sha,
        "county_manifest": sources.county_manifest,
        "manifests": sources.manifests,
        "overrides": sources.overrides,
        "prediction": sources.prediction_sha,
        "seed": sources.seed,
        "selection_list": sources.posted_files.get("selection_list"),
        "cvr_comparison": sources.posted_files.get("cvr_comparison"),
    }
    if targeted_sha is not None:
        inputs["targeted_list"] = targeted_sha
        inputs["selection_list"] = inputs["cvr_comparison"] = None
    in_rla, targeted_in = overlaps(workspace, log, name, targeted=targeted_sha is not None)
    extract = make_extract(
        county=name,
        election=sources.election,
        seed=sources.seed,
        prediction=sources.prediction,
        cvr_path=workspace.path_for(cvr_sha),
        pull_list=workspace.get_bytes(listed_sha) if listed_sha else None,
        boards=boards,
        locations=sources.locations,
        inputs=inputs,
        purpose=purpose,
        published=published,
        first=None if targeted_sha else sources.first,
        targeted=targeted_sha is not None,
        sources=source_lists if targeted_sha is not None else None,
        in_rla=in_rla,
        targeted_in=targeted_in,
    )
    body = canonical_json(extract)
    sha = hashlib.sha256(body).hexdigest()
    index = page_index(extract, sha)
    index_body = canonical_json(index)
    if skip_if_unchanged:
        previous = _latest_extract_event(log, name, purpose, targeted_key(clean_list_name(list_name)) if targeted_sha is not None else None)
        # Recorded again if the pages are defined anew (a new page format), though the extract is the same.
        if (
            previous is not None
            and previous.file_sha256 == sha
            and previous.data.get("page_index_sha256") == hashlib.sha256(index_body).hexdigest()
            and (targeted_sha is None or previous.data.get("list_record_sha256") == list_record)
        ):
            return BuiltExtract(extract, sha, previous.seq, str(previous.data.get("page_index_sha256")), dict(previous.data.get("pdfs") or {}))
    stored = workspace.put_bytes(body)
    index_stored = workspace.put_bytes(index_body)
    from corla_observer.pdfextract import render_pdf
    from corla_observer.version import software as running_software

    software = running_software()

    pdfs: dict[str, dict[str, str]] = {}
    for layout in layouts(extract):
        for board in extract_boards(extract):
            pdf = render_pdf(extract, sha, board, layout, software=software)
            pdfs.setdefault(layout, {})[str(board)] = workspace.put_bytes(pdf).sha256
    event = log.append(
        "extract.built",
        actor,
        county=name,
        file_sha256=stored.sha256,
        data={
            "sha256": stored.sha256,
            "size": stored.size,
            "format": EXTRACT_FORMAT,
            "purpose": purpose,
            "order": extract["order"],
            "board_split": extract["board_split"],
            "inputs": inputs,
            "ballots": len({b["position"] for b in extract["ballots"]}),
            "possible": len({b["position"] for b in extract["ballots"] if not b["certain"]}),
            "boundary": len({b["position"] for b in extract["ballots"] if b["boundary"]}),
            "sources_disagree": bool(extract["checks"].get("sources_disagree")),
            "other_source": extract["checks"].get("other_source", {}).get("source") if extract["checks"].get("other_source") else None,
            "boards": extract["boards"],
            "redacted": sum(1 for b in extract["ballots"] if b["status"] == "redacted"),
            "not_in_cvr": sum(1 for b in extract["ballots"] if b["status"] == "not_in_cvr"),
            "in_rla": len(extract["checks"].get("in_rla") or []),
            "previously_targeted": len(extract["checks"].get("previously_targeted") or []),
            "pages": {layout: {board: len(pages) for board, pages in part["boards"].items()} for layout, part in index["layouts"].items()},
            "page_index_sha256": index_stored.sha256,
            "pdfs": pdfs,
            "software": software,
        }
        | (
            {"list": targeted_key(clean_list_name(list_name)), "list_name": clean_list_name(list_name), "list_record_sha256": list_record, "combined": bool(source_lists)}
            if targeted_sha is not None
            else {}
        ),
    )
    return BuiltExtract(extract, stored.sha256, event.seq, index_stored.sha256, pdfs)


def overlaps(workspace: Workspace, log: EventLog, county: str, *, targeted: bool) -> tuple[set[str], dict[str, str]]:
    """What a new extract's ballots overlap with, by `id_key` (docs/TARGETING.md, "Duplicates and overlaps").

    For a targeted extract: the IDs the county's RLA extract holds (and no
    ``targeted_in``). For an RLA extract: each ID an earlier sent targeted
    list holds, with that list's label ("Targeted list 1"), the earliest
    list first (and no ``in_rla``).
    """
    if targeted:
        found = latest_extract(workspace, log, county)
        return ({id_key(str(b["imprinted_id"])) for b in found[0]["ballots"]} if found else set()), {}
    from corla_observer.pulllists import county_lists, load

    held: dict[str, str] = {}
    for item in county_lists(log.iter_events(), county, retired=True):
        if not item.targeted:
            continue
        try:
            ballots = load(workspace, item)["ballots"]
        except Exception:  # noqa: BLE001 - a list whose extract is missing from the workspace marks nothing
            continue
        for b in ballots:
            held.setdefault(id_key(str(b["imprinted_id"])), f"Targeted list {item.number}")
    return set(), held


def _latest_extract_event(log: EventLog, county: str, purpose: str, list_key: str | None = None) -> Any:
    """The county's latest ``extract.built`` of ``purpose``: its RLA extract's, or, with ``list_key``, that targeted list's."""
    found = None
    for event in log.iter_events():
        if event.kind != "extract.built" or event.county != county or event.data.get("purpose") != purpose:
            continue
        if (list_key is None and not is_targeted_event(event)) or (list_key is not None and is_targeted_event(event) and event.data.get("list") == list_key):
            found = event
    return found


def latest_extract_events(log: EventLog, *, purpose: str = "audit") -> dict[str, Any]:
    """Each county's latest ``extract.built`` event of ``purpose``, targeted extracts left out (the RLA extract's)."""
    found: dict[str, Any] = {}
    for event in log.iter_events():
        if event.kind == "extract.built" and event.county and event.data.get("purpose") == purpose and not is_targeted_event(event):
            found[event.county] = event
    return found


def extract_sha256(extract: Mapping[str, Any]) -> str:
    """The extract's hash: SHA-256 of its canonical serialization."""
    return hashlib.sha256(canonical_json(extract)).hexdigest()


def latest_extract(workspace: Workspace, log: EventLog, county: str, *, purpose: str | None = None) -> tuple[dict[str, Any], str] | None:
    """A county's RLA extract: its latest (of ``purpose``, if given) that is not targeted, and its SHA-256, or None.

    A targeted extract is one of the county's other pull lists (`corla_observer.pulllists`).
    """
    sha: str | None = None
    for event in log.iter_events():
        if event.kind == "extract.built" and event.county == county and event.file_sha256 and not is_targeted_event(event):
            if purpose is None or event.data.get("purpose") == purpose:
                sha = event.file_sha256
    if sha is None:
        return None
    data: dict[str, Any] = json.loads(workspace.get_bytes(sha))
    return data, sha


def board_ballots(extract: Mapping[str, Any], board: int) -> list[dict[str, Any]]:
    return [b for b in extract["ballots"] if b["board"] == board]


def extract_boards(extract: Mapping[str, Any]) -> list[int]:
    return sorted(int(b) for b in extract["boards"])


# ---------------------------------------------------------------------------
# Pages


def targeted_in_note(label: str) -> str:
    """The mark on an RLA sample's ballot that an earlier sent targeted list holds."""
    return f"PREVIOUSLY TARGETED: {label}, may already have been pulled"


def ballot_marks(ballot: Mapping[str, Any]) -> list[str]:
    """A ballot's marks, printed on a line of their own: POSSIBLE (`POSSIBLE_MARK`), BOUNDARY (`boundary_note`), and the overlaps (`IN_RLA_MARK`, `targeted_in_note`)."""
    marks = []
    if not ballot.get("certain", True):
        marks.append(POSSIBLE_MARK)
    if ballot.get("boundary"):
        marks.append(boundary_note(ballot["boundary"]))
    if ballot.get("source_list"):
        marks.append(f"FROM: {ballot['source_list']}")
    if ballot.get("in_rla"):
        marks.append(IN_RLA_MARK)
    if ballot.get("targeted_in"):
        marks.append(targeted_in_note(str(ballot["targeted_in"])))
    return marks


def vote_lines(value: str) -> int:
    """Printed lines of one contest's expected value: one per choice (a multi-seat contest's choices, an overvote's), at least one."""
    return max(1, len(split_value(value)[1]))


def block_lines(ballot: Mapping[str, Any], continuation: bool = True) -> int:
    """Printed lines of a ballot's block: the imprinted ID's line, one for its marks if any, plus one per contest and one more per choice after a contest's first (`vote_lines`), at least one.

    Without ``continuation`` (page formats before 6), one line per contest.
    """
    votes = ballot["votes"]
    body = sum(vote_lines(str(v)) for _, v in votes) if continuation else len(votes)
    return 1 + (1 if ballot_marks(ballot) else 0) + max(1, body)


def column_split(
    ballots: Sequence[Mapping[str, Any]], column_lines: int = PAGE_LINES, columns: int = 1, continuation: bool = True
) -> list[list[list[Mapping[str, Any]]]]:
    """Ballots into pages of ``columns`` columns, in order: a column takes ballots while their `block_lines` fit in ``column_lines``.

    A ballot that does not fit in a column with others starts the next
    column (or, after the page's last column, a new page); one longer than
    a column has a column of its own.
    """
    pages: list[list[list[Mapping[str, Any]]]] = []
    lines = 0
    for ballot in ballots:
        need = block_lines(ballot, continuation)
        if pages and lines + need <= column_lines:
            pages[-1][-1].append(ballot)
            lines += need
        elif pages and len(pages[-1]) < columns:
            pages[-1].append([ballot])
            lines = need
        else:
            pages.append([[ballot]])
            lines = need
    return pages


def paginate(ballots: Sequence[Mapping[str, Any]], page_lines: int = PAGE_LINES, columns: int = 1) -> list[list[Mapping[str, Any]]]:
    """Ballots into pages, in order (`column_split`): with one column, a page takes ballots while their `block_lines` fit in ``page_lines``."""
    return [[b for column in page for b in column] for page in column_split(ballots, page_lines, columns)]


def layouts(extract: Mapping[str, Any]) -> tuple[str, ...]:
    """The print layouts an extract's pages are defined for: both, or only portrait for a version 1 extract (page format 2)."""
    return tuple(LAYOUTS) if page_format(extract) >= 4 else ("portrait",)


def default_layout(extract: Mapping[str, Any]) -> str:
    """The layout printed when none is chosen: `DEFAULT_LAYOUT`, or "portrait" for a version 1 extract."""
    return DEFAULT_LAYOUT if page_format(extract) >= 4 else "portrait"


def _capacity(extract: Mapping[str, Any], layout: str) -> tuple[int, int]:
    if layout not in layouts(extract):
        known = ", ".join(layouts(extract))
        raise ExtractError(f"this extract's pages have no {layout} layout (they have {known})" if layout in LAYOUTS else f"layout is one of {', '.join(LAYOUTS)}")
    return LAYOUTS[layout] if page_format(extract) >= 4 else (1, PAGE_LINES_FORMAT_2)


def page_record(
    extract: Mapping[str, Any], sha256: str, board: int, page: int, pages: int, ballots: Sequence[Mapping[str, Any]], layout: str = "portrait"
) -> dict[str, Any]:
    """The record a page's hash is computed over (docs/EXTRACT.md)."""
    record: dict[str, Any] = {
        "format": page_format(extract),
        "kind": "observer_extract_page",
        "extract_sha256": sha256,
        "county": extract["county"],
        "board": board,
        "page": page,
        "pages": pages,
        "ballots": [dict(b) for b in ballots],
    }
    if record["format"] >= 4:
        record["layout"] = layout
    return record


def page_sha256(record: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(record)).hexdigest()


@dataclass(frozen=True)
class Page:
    board: int
    number: int
    pages: int
    ballots: tuple[Mapping[str, Any], ...]
    sha256: str
    layout: str = "portrait"
    #: The page's ballots by column, left to right (one column in portrait).
    columns: tuple[tuple[Mapping[str, Any], ...], ...] = ()


def board_pages(extract: Mapping[str, Any], sha256: str, board: int, layout: str | None = None) -> list[Page]:
    """A board's printed pages in ``layout`` (by default the extract's `default_layout`), each with its hash."""
    layout = layout or default_layout(extract)
    columns, lines = _capacity(extract, layout)
    split = column_split(board_ballots(extract, board), lines, columns, continuation=page_format(extract) >= 6)
    out = []
    for n, cols in enumerate(split, start=1):
        ballots = [b for column in cols for b in column]
        sha = page_sha256(page_record(extract, sha256, board, n, len(split), ballots, layout))
        out.append(Page(board, n, len(split), tuple(ballots), sha, layout, tuple(tuple(c) for c in cols)))
    return out


def _index_boards(extract: Mapping[str, Any], sha256: str, layout: str) -> dict[str, Any]:
    return {
        str(board): [
            {"page": p.number, "sha256": p.sha256, "positions": [int(b["position"]) for b in p.ballots]}
            for p in board_pages(extract, sha256, board, layout)
        ]
        for board in extract_boards(extract)
    }


def page_index(extract: Mapping[str, Any], sha256: str) -> dict[str, Any]:
    """Every page's hash and its ballots' positions, per layout and board."""
    if page_format(extract) < 4:
        return {
            "format": page_format(extract),
            "kind": "observer_extract_pages",
            "extract_sha256": sha256,
            "page_lines": PAGE_LINES_FORMAT_2,
            "boards": _index_boards(extract, sha256, "portrait"),
        }
    return {
        "format": page_format(extract),
        "kind": "observer_extract_pages",
        "extract_sha256": sha256,
        "layouts": {
            layout: {"columns": columns, "column_lines": lines, "boards": _index_boards(extract, sha256, layout)}
            for layout, (columns, lines) in LAYOUTS.items()
        },
    }


def page_hashes(extract: Mapping[str, Any], sha256: str, layout: str | None = None) -> dict[int, list[str]]:
    return {board: [p.sha256 for p in board_pages(extract, sha256, board, layout)] for board in extract_boards(extract)}


def qr_payload(page: Page) -> str:
    """A page's QR code text: ``COX2:<page SHA-256>``, in upper case (QR alphanumeric mode).

    The page's hash commits to the extract's hash (`page_record`), so it is
    all a reader needs to find the page and check it.
    """
    return f"{QR_PREFIX}:{page.sha256.upper()}"


def qr_extract_payload(extract_sha: str) -> str:
    """The provenance page's QR code text: ``COX2E:<extract SHA-256>``, in upper case."""
    return f"{QR_EXTRACT_PREFIX}:{extract_sha.upper()}"


def screen_qr_payload(extract_sha: str, board: int, board_position: int) -> str:
    """The observer screen's QR code for one ballot: ``COX2S:<first 32 hex digits of the extract's hash>:<board>:<board position>``, in upper case.

    It locates the ballot exactly (the extract, its board and its place on
    it), so a screenshot or photo of the screen can be checked against the
    committed extract (docs/PHOTO_CHECK.md). 128 bits of the hash are plenty
    to find the extract among those a workspace built, and keep the code a
    version 3 symbol (29 modules), small enough for a phone screen's corner.
    """
    return f"{QR_SCREEN_PREFIX}:{extract_sha[:SCREEN_QR_HASH].upper()}:{board}:{board_position}"


def qr_rows(text: str) -> list[str]:
    """A QR code (error correction M) as rows of hex digits, each row's modules as bits, most significant first, padded to whole hex digits: for the page to draw."""
    import segno

    code = segno.make(text, error="m", micro=False)
    rows = []
    for row in code.matrix:
        bits = "".join("1" if m else "0" for m in row)
        bits += "0" * (-len(bits) % 4)
        rows.append("".join(f"{int(bits[i : i + 4], 2):x}" for i in range(0, len(bits), 4)))
    return rows


def qr_svg(text: str, scale: int = 2, title: str = "Page hash") -> str:
    """An inline SVG QR code (error correction M), made without the network."""
    import segno

    code = segno.make(text, error="m", micro=False)
    svg: str = code.svg_inline(scale=scale, omitsize=True, svgclass="qr", lineclass="qrline", title=title)
    return svg


# ---------------------------------------------------------------------------
# Forms

#: Between the parts of an identification line.
SEPARATOR = " \N{MIDDLE DOT} "


def short(sha: object, n: int = 8) -> str:
    """A hash's first ``n`` hex digits, or "none"."""
    return str(sha)[:n] if sha else "none"


#: The extract's hashed inputs, as labelled on the provenance page and in the spreadsheet.
INPUT_LABELS = {
    "pull_list": "pull list",
    "redacted_cvr": "redacted CVR",
    "county_manifest": "county manifest",
    "manifests": "all manifests",
    "overrides": "overrides",
    "prediction": "prediction",
    "selection_list": "CORLA's selection list",
    "cvr_comparison": "CORLA's CVR comparison",
    "targeted_list": "targeted list",
}

#: How each interim order is named on a page's identification line.
ORDER_LABELS = {"predicted": "PREDICTED ORDER", "audit_center": "AUDIT CENTER ORDER", "targeted": "TARGETED (NOT RLA)"}
#: The mark on a ballot drawn only if the sample count is above its low estimate.
POSSIBLE_MARK = "POSSIBLE: drawn only if the sample count is above its low estimate"
#: The mark on a targeted list's ballot that the county's RLA sample also holds (docs/TARGETING.md).
IN_RLA_MARK = "ALSO IN THE RLA SAMPLE: entered there; no need to pull it twice"

#: How to check a printed page, on the provenance page (docs/EXTRACT.md, "Checking an extract").
CHECK_STEPS = (
    "Read the page's QR code (COX2: and the page's full hash), or its short hashes.",
    "Find that hash in the page index (published with the evidence; its hash is above) or in the spreadsheet. "
    "To check the index itself, recompute the extract from the inputs listed (each by its SHA-256, from the "
    "published evidence) and compare its hash with the extract's hash above; then recompute the board's pages "
    "in the page's layout (docs/EXTRACT.md).",
    "Compare the imprinted IDs and choices printed on the page with that page's ballots in the extract.",
)

#: How each layout is named on the provenance page.
LAYOUT_LABELS = {
    **{name: f"{name}: {lay.description}, one page per sheet" for name, lay in printlayout.LAYOUTS.items()},
    "portrait": "portrait, one page per sheet",
}


def screen_line(extract: Mapping[str, Any], sha256: str, board: int | None = None) -> str:
    """The short identification shown on screen: county, board (if given), and the extract's hash, first 8 hex digits."""
    return SEPARATOR.join([str(extract["county"]), *([f"Board {board}"] if board is not None else []), f"extract {short(sha256)}"])


def extract_software(log: EventLog, sha256: str) -> str | None:
    """The software that last built the extract with this hash, as its ``extract.built`` event records it."""
    software = None
    for event in log.iter_events():
        if event.kind == "extract.built" and event.file_sha256 == sha256:
            software = str(event.data.get("software") or "") or None
    return software


def extract_pdfs(log: EventLog, sha256: str) -> dict[str, dict[str, str]]:
    """The printouts' PDF hashes (layout, then board, to SHA-256) recorded by the latest ``extract.built`` event of the extract with this hash; empty if none."""
    pdfs: dict[str, dict[str, str]] = {}
    for event in log.iter_events():
        if event.kind == "extract.built" and event.file_sha256 == sha256:
            found = event.data.get("pdfs")
            pdfs = {str(k): {str(b): str(h) for b, h in v.items()} for k, v in found.items() if isinstance(v, dict)} if isinstance(found, dict) else {}
    return pdfs


def grouped(sha: str, n: int = 8) -> str:
    """A hash in groups of ``n`` hex digits separated by spaces, so that it can break between groups and never overflows."""
    return " ".join(sha[i : i + n] for i in range(0, len(sha), n))


@dataclass(frozen=True)
class Provenance:
    """What the provenance page lists for one board in one layout: the extract, its inputs, the page index and the key to the printed names."""

    county: str
    board: int
    order: str
    seed: str
    sha256: str
    #: ``(label, SHA-256 or None)`` per input, in `INPUT_LABELS` order.
    inputs: tuple[tuple[str, str | None], ...]
    software: str | None
    page_index_sha256: str
    layout: str
    pages: tuple[Page, ...]
    #: The board's number of pages in each layout.
    page_counts: tuple[tuple[str, int], ...]
    #: ``(short, meaning)``: the abbreviations used, and the marks.
    key: tuple[tuple[str, str], ...] = ()
    #: ``(printed, full)``: each contest printed shorter than its full name.
    shortened: tuple[tuple[str, str], ...] = ()

    @property
    def qr(self) -> str:
        return qr_extract_payload(self.sha256)


def print_names(extract: Mapping[str, Any], layout: str) -> PrintNames:
    """The printed contest and choice names of ``layout`` (a print layout, or "text")."""
    return PrintNames.for_extract(extract, layout)


def provenance(
    extract: Mapping[str, Any], sha256: str, board: int, *, software: str | None = None, layout: str | None = None, names: PrintNames | None = None
) -> Provenance:
    """A board's provenance page for ``layout``: the extract's hash, seed and inputs in full, the page index's hash, and the key to the printed names.

    It is printed before the board's first page and not counted among its
    pages, so page numbers and page hashes do not depend on it. ``names``
    are the printed names it gives the key to (by default ``layout``'s,
    by default the extract's `default_layout`).
    """
    layout = layout or default_layout(extract)
    pages = board_pages(extract, sha256, board, layout)
    if not pages:
        raise ExtractError(f"no ballots for board {board}")
    names = names or print_names(extract, layout)
    inputs = extract["inputs"]
    marks = [(POSSIBLE_MARK.split(":")[0], POSSIBLE_MARK)] if any(not b.get("certain", True) for b in pages_ballots(pages)) else []
    if any(b.get("boundary") for b in pages_ballots(pages)):
        marks.append(("BOUNDARY", "BOUNDARY: may be another board's ballot (named). If this board doesn't pull it, skip it."))
    if any(b.get("in_rla") for b in pages_ballots(pages)):
        marks.append(("ALSO IN RLA SAMPLE", IN_RLA_MARK))
    if any(b.get("targeted_in") for b in pages_ballots(pages)):
        marks.append(("PREVIOUSLY TARGETED", "PREVIOUSLY TARGETED: an earlier targeted list (named) holds the ballot, which may already have been pulled."))
    return Provenance(
        county=str(extract["county"]),
        board=board,
        order=str(extract["order"]),
        seed=str(extract["seed"] or "none"),
        sha256=sha256,
        inputs=tuple(
            (label, str(inputs[name]) if inputs.get(name) else None)
            for name, label in INPUT_LABELS.items()
            if name in inputs or name != "targeted_list"
        ),
        software=software,
        page_index_sha256=hashlib.sha256(canonical_json(page_index(extract, sha256))).hexdigest(),
        layout=layout,
        pages=tuple(pages),
        page_counts=tuple((other, len(board_pages(extract, sha256, board, other))) for other in layouts(extract)),
        key=tuple(names.key()) + tuple(marks),
        shortened=tuple(names.shortened_contests()),
    )


def pages_ballots(pages: Iterable[Page]) -> Iterator[Mapping[str, Any]]:
    for page in pages:
        yield from page.ballots


def provenance_rows(prov: Provenance) -> list[tuple[str, str]]:
    """The provenance page's facts, as ``(label, value)``; hashes in full, ungrouped."""
    others = "; ".join(f"{n} {layout}" for layout, n in prov.page_counts if layout != prov.layout)
    return [
        ("County", prov.county),
        ("Audit board", str(prov.board)),
        ("Layout", LAYOUT_LABELS.get(prov.layout, prov.layout)),
        ("Pages", f"{len(prov.pages)} (not counting this one)" + (f"; {others}" if others else "")),
        ("Order", f"{ORDER_LABELS[prov.order]} (not the county's pull list)" if prov.order in ORDER_LABELS else "the county's pull list"),
        ("Seed", prov.seed),
        ("Extract", prov.sha256),
        *((f"Input: {label}", value or "none") for label, value in prov.inputs),
        ("Software", prov.software or "not recorded"),
        ("Page index", prov.page_index_sha256),
    ]


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def is_hash(value: str) -> bool:
    return bool(_HASH_RE.match(value))


def render_provenance_text(prov: Provenance) -> str:
    """The provenance page as plain text, with the notices at its top, in lines of at most `TEXT_WIDTH` characters."""
    rows = provenance_rows(prov)
    width = max(len(label) for label, _ in rows)
    facts = []
    for label, value in rows:
        if is_hash(value) and width + 2 + len(grouped(value)) > TEXT_WIDTH:
            facts += [label, f"    {grouped(value)}"]
        elif width + 2 + len(value) > TEXT_WIDTH:
            facts += textwrap.wrap(f"{label:<{width}}  {value}", TEXT_WIDTH, subsequent_indent=" " * (width + 2))
        else:
            facts.append(f"{label:<{width}}  {value}")
    lines = [
        *textwrap.wrap(KEEP_OUT_OF_SIGHT, TEXT_WIDTH),
        *(textwrap.wrap(ORDER_NOTES[prov.order], TEXT_WIDTH) if prov.order in INTERIM_ORDERS else []),
        SEPARATOR.join([prov.county, f"Board {prov.board}", "PROVENANCE", f"extract {short(prov.sha256)}"]),
        "",
        *facts,
    ]
    def listed(items: Iterable[tuple[str, str]]) -> list[str]:
        return [line for a, b in items for line in textwrap.wrap(f"{a} = {b}", TEXT_WIDTH, initial_indent="  ", subsequent_indent="      ")]

    if prov.key:
        lines += ["", "Key:", *listed(prov.key)]
    if prov.shortened:
        lines += ["", "Contests printed shorter:", *listed(prov.shortened)]
    lines += ["", "To check a printed page:"]
    lines += [line for n, step in enumerate(CHECK_STEPS, start=1) for line in textwrap.wrap(f"{n}. {step}", TEXT_WIDTH, subsequent_indent="   ")]
    return "\n".join(lines) + "\n"


#: The width the imprinted ID's line is centred in, in the plain text.
TEXT_WIDTH = 80


def print_contest(name: str) -> str:
    """A contest's name as always printed (`corla_observer.printnames.base_contest`): without "(Vote For=1)", "(Statutory)" or "(Constitutional)", and shortened.

    "Representative to the 120th United States Congress - District 4 - REP"
    is "US Rep - CD4 - REP"; likewise "CU Regent - CDn" and "State BOE - CDn".
    A layout shortens it further if it is too long (`PrintNames`).
    """
    return base_contest(name)


def print_vote(value: str) -> str:
    """An expected value as printed: "NO VOTE", and several choices joined by ", " (an overvote and "redacted" as they are)."""
    return "NO VOTE" if value == "no vote" else value.replace(" + ", ", ")


#: Shorter forms of a redacted or missing ballot's line, for a layout too narrow for the whole.
_STATUS_SHORT = {
    "redacted in the CVR: nothing to check": ("redacted: nothing to check", "nothing to check"),
    "this ballot is not in the redacted CVR": ("not in the redacted CVR", "not in the CVR"),
}


def print_rows(ballot: Mapping[str, Any], names: PrintNames | None = None) -> list[tuple[str, str]]:
    """A ballot's printed lines below its imprinted ID: ``(contest, choices)``, or one line for a redacted or missing ballot.

    With ``names``, the contests and choices are shortened to fit that layout.
    """
    if ballot["status"] != "votes":
        if names is None:
            return vote_text(ballot)
        return [(label, names.fit_choice(text, _STATUS_SHORT.get(text, ()))) for label, text in vote_text(ballot)]
    if names is None:
        return [(print_contest(str(c)), print_vote(str(v))) for c, v in ballot["votes"]]
    return [(names.contest(str(c)), names.vote(str(v))) for c, v in ballot["votes"]]


@dataclass(frozen=True)
class PrintLine:
    """One printed line of a ballot below its imprinted ID: a contest's name and its first choice, or (``continuation``) one more of its choices."""

    contest: str
    value: str
    #: The contest's place among the ballot's votes, from 0.
    row: int
    continuation: bool = False


def print_lines(ballot: Mapping[str, Any], names: PrintNames, continuation: bool = True) -> list[PrintLine]:
    """A ballot's printed lines (`print_rows`), with a contest's several choices one per line (`PrintNames.vote_lines`): no name is cut to make room for another.

    Without ``continuation`` (page formats before 6), one line per contest, its choices joined by ", ".
    """
    if ballot["status"] != "votes" or not continuation:
        return [PrintLine(c, v, k) for k, (c, v) in enumerate(print_rows(ballot, names))]
    out: list[PrintLine] = []
    for k, (contest, value) in enumerate(ballot["votes"]):
        forms = names.vote_lines(str(value))
        out.append(PrintLine(names.contest(str(contest)), forms[0], k))
        out.extend(PrintLine("", form, k, True) for form in forms[1:])
    return out


#: Shorter forms of the marks, tried in turn until a ballot's marks fit on one line.
_MARK_FORMS: tuple[tuple[str, Callable[[Sequence[int]], str]], ...] = (
    (POSSIBLE_MARK, lambda other: boundary_note(other)),
    ("POSSIBLE: only if the count is above its low estimate", lambda other: f"BOUNDARY: may be {' or '.join(f'Board {b}' for b in other)}'s; skip it if not pulled"),
    ("POSSIBLE", lambda other: f"BOUNDARY: may be {' or '.join(f'Board {b}' for b in other)}'s"),
)


def print_marks(ballot: Mapping[str, Any], names: PrintNames, sep: str = " \N{MIDDLE DOT} ") -> str:
    """A ballot's marks (POSSIBLE, BOUNDARY) on one line that fits ``names``'s layout, or "" if none: in full if they fit, else shorter."""
    if not ballot_marks(ballot):
        return ""
    line = ""
    others = ([f"FROM: {ballot['source_list']}"] if ballot.get("source_list") else []) + ([IN_RLA_MARK] if ballot.get("in_rla") else [])
    others += [targeted_in_note(str(ballot["targeted_in"]))] if ballot.get("targeted_in") else []
    short_others = ([f"FROM: {ballot['source_list']}"] if ballot.get("source_list") else []) + (["ALSO IN RLA SAMPLE"] if ballot.get("in_rla") else [])
    short_others += [f"PREVIOUSLY TARGETED: {ballot['targeted_in']}"] if ballot.get("targeted_in") else []
    for n, (possible, boundary) in enumerate(_MARK_FORMS):
        parts = ([possible] if not ballot.get("certain", True) else []) + ([boundary(ballot["boundary"])] if ballot.get("boundary") else [])
        line = sep.join(parts + (others if n == 0 else short_others))
        if names.marks_fit(line):
            return line
    return names.cut_marks(line)


def id_line(imprinted_id: str, width: int = TEXT_WIDTH) -> str:
    """The imprinted ID's line in the plain text: ``ImprintedId 102-46-5`` centred in dashes."""
    return f" ImprintedId {imprinted_id} ".center(width, "-")


def page_of(page: Page) -> str:
    """ "page N of M" and the layout ("page 3 of 54 landscape-2"; a version 1 extract's portrait pages have none)."""
    return f"page {page.number} of {page.pages}" + (f" {page.layout}" if page.layout != "portrait" else "")


def page_line(extract: Mapping[str, Any], sha256: str, page: Page) -> str:
    """A page's identification line: county, board, page N of M and the layout, "PREDICTED ORDER" if so, and the extract's and page's hashes, first 8 hex digits.

    The seed and the inputs are on the provenance page; the extract's hash commits to them.
    """
    return SEPARATOR.join(
        [
            str(extract["county"]),
            f"Board {page.board}",
            page_of(page),
            *([ORDER_LABELS[extract["order"]]] if extract["order"] in ORDER_LABELS else []),
            f"extract {short(sha256)}",
            f"page {short(page.sha256)}",
        ]
    )


def scope_label(scope: str) -> str:
    return {"statewide": "statewide", "multi-county": "multi-county", "county": "county"}.get(scope, scope)


def vote_text(ballot: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The lines to check for a ballot: ``(contest, expected)``, or one line for a redacted or missing ballot."""
    if ballot["status"] == "redacted":
        return [("REDACTED", "redacted in the CVR: nothing to check")]
    if ballot["status"] == "not_in_cvr":
        return [("NOT IN THE CVR", "this ballot is not in the redacted CVR")]
    return [(str(c), str(v)) for c, v in ballot["votes"]]


def render_text(extract: Mapping[str, Any], sha256: str, board: int, *, software: str | None = None) -> str:
    """A board's extract as monospaced plain text, its pages those of `TEXT_LAYOUT` (one column), separated by form feeds.

    The provenance page comes first (`render_provenance_text`), with the
    notices at its top; then every page has its identification line; then,
    per ballot, its imprinted ID centred in dashes and a line per contest:
    the contest right-aligned to the page's longest, ``__`` for the check
    mark, and the choices, each short enough (`PrintNames`, layout "text")
    that no line is longer than 80 characters.
    """
    names = print_names(extract, "text")
    prov = provenance(extract, sha256, board, software=software, names=names, layout=TEXT_LAYOUT if page_format(extract) >= 4 else None)
    out = [render_provenance_text(prov)]
    for page in prov.pages:
        lines = [page_line(extract, sha256, page), ""]
        cont = page_format(extract) >= 6
        rows = {int(b["position"]): print_lines(b, names, cont) for b in page.ballots}
        width = max(len(line.contest) for r in rows.values() for line in r)
        for ballot in page.ballots:
            lines.append(id_line(str(ballot["imprinted_id"])))
            marks = print_marks(ballot, names, " * ")
            if marks:
                lines.append(" * " + marks)
            lines.extend(
                f"{'':>{width}}    {line.value}" if line.continuation else f"{line.contest:>{width}} __ {line.value}"
                for line in rows[int(ballot["position"])]
            )
        out.append("\n".join(lines) + "\n")
    return "\f".join(out)


def _templates() -> Any:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    return Environment(
        loader=FileSystemLoader(Path(__file__).parent / "web" / "templates"),
        autoescape=select_autoescape(default=True, default_for_string=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_provenance_html(
    extract: Mapping[str, Any],
    sha256: str,
    board: int,
    *,
    stylesheet: str | None = None,
    inline_css: str | None = None,
    integrity: str | None = None,
    links: Sequence[tuple[str, str]] = (),
    software: str | None = None,
) -> str:
    """A board's provenance (`provenance`) as a web page, without the notices: the seed, the extract's hash, every input's hash and the page index's hash, and the key to the printed names of the default layout.

    The printout itself is the PDF (`corla_observer.pdfextract`), whose
    first sheet is the same provenance page. The page links ``stylesheet``
    (with ``integrity``), or embeds ``inline_css``.
    """
    from markupsafe import Markup

    prov = provenance(extract, sha256, board, software=software)
    return str(
        _templates()
        .get_template("extract_print.html")
        .render(
            extract=extract,
            board=board,
            prov=prov,
            prov_rows=[(label, value, is_hash(value)) for label, value in provenance_rows(prov)],
            grouped=grouped,
            prov_head=SEPARATOR.join([prov.county, f"Board {board}", "PROVENANCE", f"extract {short(sha256)}"]),
            prov_qr=Markup(qr_svg(prov.qr, title="Extract hash")),
            check_steps=CHECK_STEPS,
            stylesheet=stylesheet,
            integrity=integrity,
            inline_css=Markup(inline_css) if inline_css else None,
            links=list(links),
        )
    )


def spreadsheet_cell(value: object) -> str:
    """A value for a spreadsheet cell, with a leading quote if it could be read as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


CSV_COLUMNS = (
    "board",
    "ballot on board",
    "position in county",
    "imprinted id",
    "location",
    "status",
    "certain or possible",
    "boundary",
    "drawn for",
    "expected votes",
    "match",
    "heard instead",
    *(column for layout in LAYOUTS for column in (f"page ({layout})", f"page hash ({layout})")),
)


def to_csv(extract: Mapping[str, Any], sha256: str, board: int | None = None, pdfs: Mapping[str, Mapping[str, str]] | None = None) -> str:
    """The extract (one board, or all) as CSV: identification rows (with the seed and every hash in full), a blank row, then one row per ballot, with its page and page hash in each layout.

    ``pdfs`` (layout, then board, to SHA-256: the ``pdfs`` of its
    ``extract.built`` event) adds a row per printout's PDF hash.
    """
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")

    def row(values: Iterable[object]) -> None:
        writer.writerow([spreadsheet_cell(v) for v in values])

    row(["County", extract["county"]])
    row(["Notice", KEEP_OUT_OF_SIGHT])
    if extract["order"] in INTERIM_ORDERS:
        row(["Order", ORDER_NOTES[extract["order"]]])
    row(["Seed", extract["seed"] or "none"])
    for name, label in INPUT_LABELS.items():
        row([f"Input: {label}", extract["inputs"].get(name) or "none"])
    row(["Extract", sha256])
    row(["Page index", hashlib.sha256(canonical_json(page_index(extract, sha256))).hexdigest()])
    for layout, boards in (pdfs or {}).items():
        for pdf_board, pdf_sha in sorted(boards.items(), key=lambda kv: int(kv[0])):
            if board is None or int(pdf_board) == board:
                row([f"PDF: {layout}, board {pdf_board}", pdf_sha])
    row([])
    row(CSV_COLUMNS)
    ballots = extract["ballots"] if board is None else board_ballots(extract, board)
    totals = {int(b): n for b, n in extract["boards"].items()}
    # Each ballot's page and page hash in each layout, by (board, place on the board).
    on_page: dict[str, dict[tuple[int, int], Page]] = {layout: {} for layout in layouts(extract)}
    for layout in layouts(extract):
        for number in {int(b["board"]) for b in ballots}:
            for page in board_pages(extract, sha256, number, layout):
                on_page[layout].update({(number, int(b["board_position"])): page for b in page.ballots})
    for b in ballots:
        found = [
            on_page.get(layout, on_page.get("portrait", {}) if layout == TEXT_LAYOUT else {}).get((int(b["board"]), int(b["board_position"])))
            for layout in LAYOUTS
        ]
        row(
            [
                b["board"],
                f"{b['board_position']} of {totals[int(b['board'])]}",
                b["position"],
                b["imprinted_id"],
                b.get("location") or "",
                b["status"],
                "certain" if b.get("certain", True) else "possible",
                boundary_note(b["boundary"]) if b.get("boundary") else "",
                "; ".join(f"{c} ({scope_label(s)})" for c, s in b["picks"]),
                " | ".join(f"{c}: {v}" for c, v in vote_text(b)),
                "",
                "",
                *(cell for page in found for cell in ((page.number, page.sha256) if page else ("", ""))),
            ]
        )
    return out.getvalue()
