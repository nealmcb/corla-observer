"""Each county's status in the audit and in observation, in plain words, with where each part comes from.

A county's status (`CountyAuditStatus`) combines three sides:

**CORLA's side** (`corla_uploads`): whether CORLA's upload status, the
``uploadStatus.csv`` or ``upload_status.csv`` the audit center publishes
(the latest version fetched), records the county's ballot manifest and its
CVR as ``IMPORTED``, and when, in Colorado time (CORLA writes Colorado
wall-clock times without a zone). For each of the two, the latest row
marked ``IMPORTED`` is kept, or, if there is none, the latest row of that
type, so that a failed upload shows as such. The change flags judge the
rows themselves (`corla_observer.flags`); this only says what arrived.

**Our side** (`corla_observer.uploads.status`): whether the county's
coordinator provided its redacted CVR and it passed the redaction check,
our copy of its manifest, its pull list, and the number of audit boards (a
count recorded as a placeholder is shown as not yet known).

**The audit's stage** (`audit_rounds`, `stage`): the current round, from
the audit center files in the data map that name a round, by their
directory (``round_1/``, ``round2/``) or their name
(``ContestMarginAndContestCVR-IDs-Round1.csv``), but never a manifest or a
page (the 2020 general posted its manifests under ``round_1/`` before the
roll), with CORLA's selections
counting as round 1: the highest round with a file for the county, or else
the highest round of the election (the audit center has published some
rounds' files for all counties together). Without such files, the first
round's recorded start, or the county's pull list, means round 1; otherwise
the seed's entry means the drawing is done, and without it the audit is
before the drawing. Observation
progress comes from the observers' entries (`corla_observer.practice`):
ballots entered of the total, per board.

A county takes part in observation (``participating``) if it provided a
redacted CVR to us, or has an observer or reader link, or observers'
entries. San Juan County counts its ballots by hand and does not take part
in CORLA's audit (`NOT_IN_CORLA`).

Nothing here reads a CVR's votes, and no flag is part of a status, so a
status can be shown on public pages. Standard library only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from corla_observer.classify import COLORADO_COUNTIES, Classification, canonical_county
from corla_observer.datamap import DataMap, MapEntry, Sourced, column, role_column, round_of, table_rows
from corla_observer.events import Event
from corla_observer.localtime import colorado_wall_time
from corla_observer.practice import BoardProgress
from corla_observer.sampling import county_key
from corla_observer.uploads import CountyStatus
from corla_observer.workspace import ObjectNotFoundError, Workspace

#: Counties that do not take part in CORLA's audit, with the note shown for each.
NOT_IN_CORLA = {
    "San Juan": "San Juan County counts its ballots by hand and does not take part in CORLA's risk-limiting audit.",
}

BEFORE_DRAWING = "before the drawing"
DRAWING_DONE = "drawing done (seed entered)"
BOARDS_NOT_KNOWN = "audit boards: not yet known"

_RLA = "https://www.coloradosos.gov/pubs/elections/RLA/"


def _short(ref: str) -> str:
    return "RLA/" + ref[len(_RLA) :] if ref.startswith(_RLA) else ref


# ---------------------------------------------------------------------------
# CORLA's side


@dataclass(frozen=True)
class CorlaUpload:
    """One row of CORLA's upload status: a county's manifest or CVR upload."""

    #: The file type as CORLA wrote it (``manifest``, ``CVRs``).
    type: str
    #: CORLA's status (``IMPORTED``, ``FAILED``, ``HASH_MISMATCH``, ...).
    status: str
    #: The time as CORLA wrote it, Colorado wall-clock time without a zone.
    timestamp: str
    #: The same time shown with its zone (``2026-11-03 14:02:11 MST``).
    time: str

    @property
    def imported(self) -> bool:
        return self.status == "IMPORTED"


@dataclass(frozen=True)
class CorlaCounty:
    """What CORLA's upload status records for one county."""

    county: str
    manifest: CorlaUpload | None = None
    cvr: CorlaUpload | None = None


@dataclass(frozen=True)
class CorlaUploads:
    """CORLA's upload status as read from the latest version fetched; ``source`` None if none was fetched."""

    source: str | None = None
    counties: Mapping[str, CorlaCounty] = field(default_factory=dict)


def _upload_kind(file_type: str) -> str | None:
    lowered = file_type.lower()
    if "manifest" in lowered:
        return "manifest"
    if "cvr" in lowered:
        return "cvr"
    return None


def corla_uploads(datamap: DataMap, workspace: Workspace) -> CorlaUploads:
    """Per county, CORLA's import of its manifest and CVR, from the data map's latest upload status file."""
    files = datamap.used("upload_status")
    if not files:
        return CorlaUploads()
    entry = max(files, key=lambda e: (e.seq or 0, e.ref))
    try:
        data = workspace.get_bytes(entry.sha256)
    except ObjectNotFoundError:
        return CorlaUploads()
    classification = Classification(entry.classified_kind, entry.classified_county, entry.evidence)
    header, rows = table_rows(data, classification)
    county_col = role_column(classification, "county")
    type_col = role_column(classification, "type") or column(header, "type", "file type")
    status_col = role_column(classification, "status")
    time_col = column(header, "timestamp", "time")
    found: dict[str, dict[str, CorlaUpload]] = {}
    if county_col and type_col and status_col:
        for row in sorted(rows, key=lambda r: r.get(time_col or "", "")):
            county = canonical_county(row.get(county_col, ""))
            kind = _upload_kind(row.get(type_col, ""))
            if not county or not kind:
                continue
            given = row.get(time_col or "", "").strip()
            upload = CorlaUpload(
                type=row.get(type_col, "").strip(),
                status=row.get(status_col, "").strip().upper(),
                timestamp=given,
                time=colorado_wall_time(given) if given else "",
            )
            kept = found.setdefault(county, {})
            previous = kept.get(kind)
            # An import is not undone by a later failed upload: keep the latest import if there is one.
            if previous is None or upload.imported or not previous.imported:
                kept[kind] = upload
    return CorlaUploads(
        source=_short(entry.ref),
        counties={c: CorlaCounty(c, k.get("manifest"), k.get("cvr")) for c, k in sorted(found.items())},
    )


# ---------------------------------------------------------------------------
# The audit's stage


#: Kinds of file that say nothing of a round even in a round's directory: the 2020 general posted the
#: counties' manifests, and its county manifest page, under ``round_1/`` before the roll.
NOT_ROUND_EVIDENCE = frozenset({"county_manifest", "html_page"})


@dataclass(frozen=True)
class Rounds:
    """The highest round with audit center files, for the election and per county, with a file for each."""

    election_round: int | None = None
    election_ref: str | None = None
    by_county: Mapping[str, tuple[int, str]] = field(default_factory=dict)


def audit_rounds(datamap: DataMap) -> Rounds:
    """The rounds shown by the data map's fetched audit center files in round directories."""
    best: tuple[int, str] | None = None
    by_county: dict[str, tuple[int, str]] = {}
    for entry in datamap.entries:
        if entry.source != "fetch" or entry.ignored or not _same_election(entry, datamap):
            continue
        if entry.kind in NOT_ROUND_EVIDENCE:
            continue
        number = round_of(entry.ref)
        if number is None and entry.kind == "selection_list":
            number = 1  # CORLA's selections are published with the first round
        if number is None or number < 1:
            continue
        item = (number, _short(entry.ref))
        if best is None or item > best:
            best = item
        if entry.county:
            if entry.county not in by_county or item > by_county[entry.county]:
                by_county[entry.county] = item
    return Rounds(
        election_round=best[0] if best else None, election_ref=best[1] if best else None, by_county=by_county
    )


def _same_election(entry: MapEntry, datamap: DataMap) -> bool:
    return datamap.election is None or entry.election is None or entry.election == datamap.election


@dataclass(frozen=True)
class Stage:
    """Where a county's audit is: a round, the drawing done, or before the drawing; with its source."""

    label: str
    source: str
    round: int | None = None


def stage(
    county: str, rounds: Rounds, *, seed_source: str | None, round_start: Sourced | None, pull_list: bool = False
) -> Stage:
    """A county's stage (see the module documentation)."""
    if county in rounds.by_county:
        number, ref = rounds.by_county[county]
        return Stage(f"Round {number}", f"CORLA's round {number} files for the county ({ref})", number)
    if rounds.election_round is not None:
        number = rounds.election_round
        return Stage(f"Round {number}", f"CORLA's round {number} files ({rounds.election_ref})", number)
    if round_start is not None:
        return Stage("Round 1", f"the first round's start: {round_start.source}", 1)
    if pull_list:
        return Stage("Round 1", "the county's pull list, provided to us", 1)
    if seed_source is not None:
        return Stage(DRAWING_DONE, f"the seed: {seed_source}")
    return Stage(BEFORE_DRAWING, "no seed entered, no round files published")


# ---------------------------------------------------------------------------
# Observation


def active_links(events: Iterable[Event]) -> dict[str, int]:
    """Per county, the observer and reader links made and not revoked, from the link events."""
    counts: dict[tuple[str, str, str, str], int] = {}
    for event in events:
        if event.kind not in ("practice.link_created", "practice.link_revoked") or not event.county:
            continue
        key = (event.county, str(event.data.get("role")), str(event.data.get("actor")), str(event.board))
        counts[key] = counts.get(key, 0) + (1 if event.kind == "practice.link_created" else -1)
    result: dict[str, int] = {}
    for (county, *_), n in counts.items():
        if n > 0:
            result[county] = result.get(county, 0) + n
    return result


@dataclass(frozen=True)
class BoardLine:
    """One audit board's observation progress: ballots entered, of the total if known."""

    board: str
    done: int
    ballots: int | None
    observers: int

    @property
    def text(self) -> str:
        total = f" of {self.ballots}" if self.ballots is not None else ""
        return f"board {self.board}: {self.done}{total} ballots entered"


# ---------------------------------------------------------------------------
# The status


@dataclass
class CountyAuditStatus:
    """One county's status; see the module documentation. Each ``*_source`` says where a part comes from."""

    county: str
    summary: str
    stage: str | None
    stage_source: str | None
    round: int | None
    #: CORLA's side, from its upload status (``corla_source``, None if not fetched).
    corla_source: str | None
    corla_manifest: CorlaUpload | None
    corla_cvr: CorlaUpload | None
    #: Our side, from the coordinators' uploads.
    cvr_provided: bool
    cvr_verdict: str | None
    manifest_copy: bool
    pull_list: bool
    audit_boards: int | None
    audit_boards_placeholder: bool
    audit_boards_text: str
    #: Observation.
    boards: list[BoardLine]
    links: int
    observers: int
    participating: bool
    in_corla: bool
    note: str | None = None


def _corla_phrase(manifest: CorlaUpload | None, cvr: CorlaUpload | None) -> str:
    m, c = bool(manifest and manifest.imported), bool(cvr and cvr.imported)
    if m and c:
        return "manifest and CVR uploaded to CORLA"
    if m:
        return "manifest uploaded to CORLA, CVR not yet"
    if c:
        return "CVR uploaded to CORLA, manifest not yet"
    return "manifest and CVR not yet uploaded to CORLA"


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def county_status(
    county: str,
    ours: CountyStatus | None,
    corla: CorlaUploads,
    rounds: Rounds,
    *,
    seed_source: str | None = None,
    round_start: Sourced | None = None,
    boards: Sequence[BoardProgress] = (),
    links: int = 0,
) -> CountyAuditStatus:
    """One county's status, with its one-line summary."""
    latest = ours.latest if ours is not None else {}
    cvr = latest.get("redacted_cvr")
    cvr_passed = cvr is not None and cvr.verdict == "PASS"
    placeholder = bool(ours and ours.audit_boards_placeholder)
    audit_boards = ours.audit_boards if ours is not None else None
    boards_text = (
        BOARDS_NOT_KNOWN if audit_boards is None or placeholder else f"audit boards: {audit_boards}"
    )
    lines = [
        BoardLine(b.board, b.done, b.ballots, len(b.observers))
        for b in boards
        if b.county == county
    ]
    observers = len({o for b in boards if b.county == county for o in b.observers})
    in_corla = county not in NOT_IN_CORLA
    record = corla.counties.get(county)
    manifest_row = record.manifest if record else None
    cvr_row = record.cvr if record else None

    if in_corla:
        where = stage(county, rounds, seed_source=seed_source, round_start=round_start, pull_list="pull_list" in latest)
        parts = [_capital(where.label)]
        if corla.source is not None:
            parts.append(_corla_phrase(manifest_row, cvr_row))
    else:
        where = None
        parts = ["Counts by hand; not part of CORLA's audit"]
    if cvr is None:
        if in_corla:
            parts.append("redacted CVR not yet provided")
    elif cvr_passed:
        parts.append("redacted CVR provided")
    else:
        parts.append(f"redacted CVR received, redaction check: {cvr.verdict or 'not checked'}")
    done = sum(line.done for line in lines)
    if lines and (done or any(line.ballots for line in lines)):
        totals = [line.ballots for line in lines]
        if all(t is not None for t in totals):
            parts.append(f"{done} of {sum(t or 0 for t in totals)} ballots observed")
        else:
            parts.append(f"{done} ballots observed")

    return CountyAuditStatus(
        county=county,
        summary=" · ".join(parts),
        stage=where.label if where else None,
        stage_source=where.source if where else None,
        round=where.round if where else None,
        corla_source=corla.source,
        corla_manifest=manifest_row,
        corla_cvr=cvr_row,
        cvr_provided=cvr is not None,
        cvr_verdict=cvr.verdict if cvr is not None else None,
        manifest_copy="manifest" in latest,
        pull_list="pull_list" in latest,
        audit_boards=audit_boards,
        audit_boards_placeholder=placeholder,
        audit_boards_text=boards_text,
        boards=lines,
        links=links,
        observers=observers,
        participating=cvr is not None or links > 0 or observers > 0,
        in_corla=in_corla,
        note=NOT_IN_CORLA.get(county),
    )


def county_statuses(
    ours: Mapping[str, CountyStatus],
    corla: CorlaUploads,
    rounds: Rounds,
    *,
    seed_source: str | None = None,
    round_start: Sourced | None = None,
    boards: Sequence[BoardProgress] = (),
    links: Mapping[str, int] | None = None,
    counties: Iterable[str] = COLORADO_COUNTIES,
) -> dict[str, CountyAuditStatus]:
    """Every county's status, by name in county order: all of ``counties``, and any other with an upload."""
    links = links or {}
    names = set(counties) | set(ours)
    return {
        name: county_status(
            name,
            ours.get(name),
            corla,
            rounds,
            seed_source=seed_source,
            round_start=round_start,
            boards=boards,
            links=links.get(name, 0),
        )
        for name in sorted(names, key=county_key)
    }
