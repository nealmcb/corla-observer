"""What the dashboard and the coordinator pages show, built from the event log and the data map.

`build_snapshot` reads the whole log once and builds the data map, so its
result is cached by the log's head (`SnapshotCache`): it is recomputed only
when an event has been appended. Everything in a `Snapshot` can be
recomputed by anyone from the published log and workspace.

Each county's status (`corla_observer.county_status`: CORLA's imports of
its manifest and CVR, what has arrived here, its round or stage, and
observation progress) is in ``statuses`` for every Colorado county, and on
each `CountyRow`; ``active_counties`` are the ones taking part in
observation, which the public dashboard lists.

A snapshot holds, for a redacted CVR, only its redaction verdict and counts
(rows, ballots, styles, groups, rare groups), never its contents. The
change flags, with whether a coordinator has addressed each, and the data
map problems that report the same findings, are for the coordinator pages
only: the public dashboard (`corla_observer.web.public`) shows
``public_problems`` and ``public_fetches`` instead.

No web dependencies.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from corla_observer.classify import canonical_county, is_test_county
from corla_observer.county_status import (
    CorlaUploads,
    CountyAuditStatus,
    Rounds,
    active_links,
    audit_rounds,
    corla_uploads,
    county_statuses,
)
from corla_observer.datamap import DataMap, build_map, describe
from corla_observer.events import Event, EventLog, JSONValue, format_server_time
from corla_observer.flags import (
    ADDRESSED_KIND,
    PROBLEM_FLAG_REASONS,
    REOPENED_KIND,
    Addressed,
    addressed_flags,
    addressing,
    check_flags,
    unflagged_problems,
)
from corla_observer.practice import OBSERVER_KINDS, BoardProgress, ObserverStats, practice_stats
from corla_observer.pulllists import LIST_KINDS, RLA, all_lists
from corla_observer.pulllists import load as load_list
from corla_observer.uploads import CountyStatus
from corla_observer.uploads import status as upload_status
from corla_observer.workspace import Workspace

FlagChecker = Callable[[EventLog, Workspace, DataMap], list[Any]]


#: Fetch events listed as recent activity (not every per-URL ``fetch.file``
#: and ``fetch.unchanged``, which would swamp it).
RECENT_FETCH_KINDS = frozenset(
    {
        "fetch.crawl_started",
        "fetch.crawl_finished",
        "fetch.new",
        "fetch.changed",
        "fetch.missing",
        "fetch.alert",
        "fetch.slowed",
        "coordinator.fetch_requested",
    }
)
RECENT_FETCH_COUNT = 25

#: Events of live observation, which never change the data map or the flags.
LIVE_KINDS = OBSERVER_KINDS | frozenset(
    {
        "observer.device_registered",
        "observation.rejected",
        "receipt.key_published",
        "practice.read",
        "practice.read_reset",
        "practice.link_created",
        "practice.link_revoked",
        "photo.checked",
        "coordinator.login",
        "coordinator.logout",
        "coordinator.login_failed",
        ADDRESSED_KIND,
        REOPENED_KIND,
    }
) | LIST_KINDS

#: The redaction report's counts shown for a redacted CVR.
REDACTION_COUNTS = (
    "rows_read",
    "ballot_rows",
    "already_redacted_rows",
    "aggregate_rows",
    "styles",
    "groups",
    "rare_group_count",
)

_RLA = "https://www.coloradosos.gov/pubs/elections/RLA/"


def short_ref(ref: str) -> str:
    """An audit center URL as ``RLA/...``; anything else unchanged."""
    return "RLA/" + ref[len(_RLA) :] if ref.startswith(_RLA) else ref


@dataclass
class CountyRow:
    county: str
    cvr: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    #: The manifest the data map chose for the county (fetched or uploaded).
    map_manifest: dict[str, Any] | None = None
    pull_list: dict[str, Any] | None = None
    audit_boards: int | None = None
    audit_boards_placeholder: bool = False
    rejected: int = 0
    last_rejection: str | None = None
    missing: list[str] = field(default_factory=list)
    flags: int = 0
    #: The county's status in the audit and in observation (`corla_observer.county_status`).
    status: CountyAuditStatus | None = None


@dataclass
class TargetedSummary:
    """One targeted list of a county (`corla_observer.pulllists`): its creation and its observation progress, kept apart from the RLA sample's.

    ``name`` is for coordinators; the public sees ``public_label`` ("Targeted
    list N") and the counts, and ``published`` only once a coordinator has
    published the list's details.
    """

    county: str
    key: str
    name: str
    number: int
    public_label: str
    built: str
    actor: str
    ballots: int
    sha256: str
    retired: bool
    combined: bool
    published: dict[str, Any] | None
    boards: list[BoardProgress] = field(default_factory=list)
    done: int = 0
    matches: int = 0
    notes: int = 0
    observers: list[str] = field(default_factory=list)
    #: Each observer's entry modes ("two taps", "contest by contest").
    modes: dict[str, str] = field(default_factory=dict)
    #: Ballots the RLA sample also holds (docs/TARGETING.md), and how many of those were entered on the RLA sample, which counts for this list.
    in_rla: int = 0
    done_on_rla: int = 0

    @property
    def label(self) -> str:
        default = f"Targeted list {self.number}"
        return default if self.name == default else f"{default} ({self.name})"

    def public_dict(self) -> dict[str, Any]:
        """What the public dashboard shows: the neutral label, when, the counts and the extract's short hash; the details only if published."""
        return {
            "county": self.county,
            "label": self.public_label,
            "built": self.built,
            "ballots": self.ballots,
            "extract": self.sha256[:8],
            "done": self.done,
            "matches": self.matches,
            "notes": self.notes,
            "observers": len(self.observers),
            "per_contest": sum(1 for m in self.modes.values() if "contest by contest" in m),
            "in_rla": self.in_rla,
            "details": self.published,
        }


@dataclass
class LookupSummary:
    """A county's box-targeted lookups (`corla_observer.pulllists.LOOKUP`): ballots looked up and entered, apart from every list."""

    county: str
    done: int = 0
    matches: int = 0
    notes: int = 0
    observers: list[str] = field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        return {"county": self.county, "label": "Targeted from boxes", "done": self.done, "matches": self.matches, "notes": self.notes, "observers": len(self.observers)}


def lookup_summaries(boards: list[BoardProgress]) -> list[LookupSummary]:
    """Each county's lookups, from its boards' progress on the lookup key."""
    found: dict[str, LookupSummary] = {}
    for b in boards:
        if not b.lookup:
            continue
        summary = found.setdefault(b.county, LookupSummary(b.county))
        summary.done += b.done
        summary.matches += b.matches
        summary.notes += b.notes
        summary.observers = sorted(set(summary.observers) | set(b.observers))
    return [found[c] for c in sorted(found)]


def targeted_summaries(
    events: Iterable[Event], boards: list[BoardProgress], observers: list[ObserverStats], workspace: Workspace | None = None
) -> list[TargetedSummary]:
    """Every county's targeted lists, retired ones too, by county and in the order first built.

    With ``workspace``, each list's extract is read for the ballots the RLA
    sample also holds, whose RLA entries count for the list.
    """
    found = []
    for county, lists in sorted(all_lists(events).items()):
        rla_done = {i for b in boards if b.county == county and b.list_key == RLA for i in b.done_ids}
        for item in lists:
            if not item.targeted:
                continue
            summary = TargetedSummary(
                county, item.key, item.name, item.number, item.public_label, item.built, item.actor, item.ballots, item.sha256,
                item.retired, item.combined, item.published,
            )  # fmt: skip
            summary.boards = [b for b in boards if b.county == county and b.list_key == item.key]
            summary.done = sum(b.done for b in summary.boards)
            summary.matches = sum(b.matches for b in summary.boards)
            summary.notes = sum(b.notes for b in summary.boards)
            summary.observers = sorted({o for b in summary.boards for o in b.observers})
            summary.modes = {o.actor: o.mode_text for o in observers if o.county == county and o.list_key == item.key}
            if workspace is not None:
                try:
                    shared = [str(b["imprinted_id"]) for b in load_list(workspace, item)["ballots"] if b.get("in_rla")]
                except Exception:  # noqa: BLE001 - an extract missing from the workspace: no overlap to count
                    shared = []
                done_here = {i for b in summary.boards for i in b.done_ids}
                summary.in_rla = len(set(shared))
                summary.done_on_rla = sum(1 for i in set(shared) if i in rla_done and i not in done_here)
                summary.done += summary.done_on_rla
            found.append(summary)
    return found


@dataclass
class Snapshot:
    head_seq: int
    head_hash: str
    built_at: str
    election: str | None = None
    seed: str | None = None
    seed_source: str | None = None
    roll_time: str | None = None
    counties: list[CountyRow] = field(default_factory=list)
    #: Every flag (coordinators only), each with ``addressed`` (the marking, or
    #: None) and ``reopened_by_new`` (addressed before, with new findings since).
    flags: list[dict[str, Any]] = field(default_factory=list)
    #: The data map's problems that no flag reports and that are due (coordinators only),
    #: as lines (`corla_observer.due.ProblemGroup`): one per problem many counties share.
    problems: list[dict[str, Any]] = field(default_factory=list)
    #: The problems the public sees: none that reports what a flag reports.
    public_problems: list[dict[str, Any]] = field(default_factory=list)
    #: The inputs not yet expected (`corla_observer.due.Waiting`), and status lines (election night results).
    waiting: list[dict[str, Any]] = field(default_factory=list)
    status_lines: list[str] = field(default_factory=list)
    #: The time the problems were judged at (UTC) and what it is.
    as_of: str | None = None
    as_of_source: str = ""
    #: When the view changes by itself (the next due time), so the cache rebuilds then.
    valid_until: str | None = None
    #: Due texts of milestones not yet reached (``tabulation_deadline``: ``Fri 13 Nov 2:00 PM``).
    upcoming: dict[str, str] = field(default_factory=dict)
    #: The individual problems due: all, and errors (coordinators' view), and the public's errors.
    problem_count: int = 0
    error_count: int = 0
    public_error_count: int = 0
    #: The raw problems, before staging (kept for reuse with the data map).
    raw_problems: list[Any] = field(default_factory=list)
    raw_public_problems: list[Any] = field(default_factory=list)
    #: The coordinators' view of the problems (`corla_observer.due.ProblemView`).
    view: Any = None
    recent_fetches: list[dict[str, Any]] = field(default_factory=list)
    #: Recent fetch activity without the fetcher's alerts, for the public.
    public_fetches: list[dict[str, Any]] = field(default_factory=list)
    #: Flag markings as addressed, by flag key.
    addressed: dict[str, Addressed] = field(default_factory=dict)
    manifests_in_map: int = 0
    contests: int = 0
    #: Set if the data map or the flags could not be built; the rest still shows.
    errors: list[str] = field(default_factory=list)
    #: Kept for the coordinator pages; not shown on the public dashboard.
    datamap: DataMap | None = None
    flag_objects: list[Any] = field(default_factory=list)
    map_errors: list[str] = field(default_factory=list)
    #: The seq of the last event that can change the data map (not in `LIVE_KINDS`).
    map_seq: int = 0
    #: Live observation: each board's progress, and each observer's statistics.
    #: A test county's boards (`corla_observer.classify.TEST_COUNTIES`) are in
    #: ``test_boards``, for the coordinators' statistics only: never on the dashboard.
    boards: list[BoardProgress] = field(default_factory=list)
    test_boards: list[BoardProgress] = field(default_factory=list)
    #: Each county's targeted lists (test counties' too, and retired ones), for the coordinators;
    #: ``public_targeted``, real counties' lists not retired, for the dashboard (`TargetedSummary.public_dict`).
    targeted: list[TargetedSummary] = field(default_factory=list)
    public_targeted: list[TargetedSummary] = field(default_factory=list)
    #: Each county's box-targeted lookups (`LookupSummary`), and the real counties' for the dashboard.
    lookups: list[LookupSummary] = field(default_factory=list)
    public_lookups: list[LookupSummary] = field(default_factory=list)
    #: Per county, how many of the RLA sample's ballots an earlier sent targeted list holds (its latest RLA extract's ``previously_targeted``).
    previously_targeted: dict[str, int] = field(default_factory=dict)
    #: A test county's uploads, for the coordinators' home only: never on the dashboard or in the data map.
    test_counties: list[CountyRow] = field(default_factory=list)
    observer_stats: list[ObserverStats] = field(default_factory=list)
    #: CORLA's upload status and the rounds, read with the data map and reused with it.
    corla: CorlaUploads = field(default_factory=CorlaUploads)
    rounds: Rounds = field(default_factory=Rounds)
    #: Every Colorado county's status (and any other county with an upload), in county order.
    statuses: dict[str, CountyAuditStatus] = field(default_factory=dict)
    #: Per county, what is not yet expected from it, with its due text (``cvr``, ``corla``).
    not_yet: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def active_counties(self) -> list[CountyRow]:
        """The counties taking part in observation, for the public dashboard."""
        return [row for row in self.counties if row.status is not None and row.status.participating]

    @property
    def open_flags(self) -> list[dict[str, Any]]:
        return [f for f in self.flags if not f.get("addressed_holds")]

    @property
    def done_flags(self) -> list[dict[str, Any]]:
        return [f for f in self.flags if f.get("addressed_holds")]

    @property
    def high_flags(self) -> int:
        """Open high-severity flags."""
        return sum(1 for f in self.open_flags if f.get("severity") == "high")


def _fetch_summary(event: Event) -> dict[str, Any]:
    data = event.data
    url = data.get("url")
    item: dict[str, Any] = {
        "seq": event.seq,
        "time": event.server_time,
        "kind": event.kind,
        "what": event.kind.split(".", 1)[1].replace("_", " "),
        "url": short_ref(url) if isinstance(url, str) else "",
        "detail": "",
        "severity": None,
    }
    if event.kind == "fetch.crawl_finished":
        counts = data.get("counts")
        if isinstance(counts, dict):
            item["detail"] = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items())
        if data.get("start_failed"):
            item["detail"] = "start page could not be fetched; " + item["detail"]
    elif event.kind == "fetch.alert":
        item["detail"] = str(data.get("reason", ""))
        severity = data.get("severity")
        item["severity"] = severity if isinstance(severity, str) else "normal"
    elif event.kind == "fetch.missing":
        item["detail"] = str(data.get("reason", "")) if data.get("reason") else ""
    elif event.kind == "coordinator.fetch_requested":
        item["detail"] = f"requested by {event.actor}"
    return item


def _counts(report: JSONValue) -> dict[str, Any]:
    if not isinstance(report, dict):
        return {}
    counts: dict[str, Any] = {k: report.get(k) for k in REDACTION_COUNTS if isinstance(report.get(k), int)}
    grouped = report.get("grouped_by")
    if isinstance(grouped, str):
        counts["grouped_by"] = grouped.replace("_", " ")
    return counts


def build_snapshot(
    log: EventLog,
    workspace: Workspace,
    counties: Iterable[str] = (),
    *,
    flag_checker: FlagChecker | None = None,
    previous: Snapshot | None = None,
    now: str | None = None,
) -> Snapshot:
    """Everything the dashboard shows, as of the log's current head.

    With ``previous``, the data map and the flags are reused from it if only
    live observation events (`LIVE_KINDS`) have been appended since, since
    building them takes a while and observers' taps come quickly.

    The problems are judged as of ``now`` (UTC), by default the real time or
    a walkthrough's simulated time (`corla_observer.due.clock`).
    """
    head_seq, head_hash = log.head()
    snap = Snapshot(head_seq=head_seq, head_hash=head_hash, built_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))

    reports: dict[str, JSONValue] = {}
    pull_lists: dict[str, dict[str, Any]] = {}
    recent: list[dict[str, Any]] = []
    live: list[Event] = []
    markings: list[Event] = []
    for event in log.iter_events(page_size=5000):
        if event.seq > head_seq:
            break
        if event.kind in (ADDRESSED_KIND, REOPENED_KIND):
            markings.append(event)
            continue
        if event.kind in LIVE_KINDS:
            live.append(event)
            continue
        snap.map_seq = event.seq
        if event.kind in ("extract.built", "practice.extract_built"):
            live.append(event)
        if event.kind == "redaction.checked" and event.file_sha256:
            reports[event.file_sha256] = event.data.get("report")
        elif event.kind == "file.classified" and event.file_sha256 and event.data.get("kind") == "pull_list":
            info = event.data.get("pull_list")
            pull_lists[event.file_sha256] = dict(info) if isinstance(info, dict) else {}
        elif event.kind in RECENT_FETCH_KINDS:
            recent.append(_fetch_summary(event))
            if len(recent) > RECENT_FETCH_COUNT * 4:
                del recent[: -RECENT_FETCH_COUNT]
    snap.recent_fetches = list(reversed(recent[-RECENT_FETCH_COUNT:]))
    snap.public_fetches = [f for f in snap.recent_fetches if f["kind"] != "fetch.alert"]
    snap.addressed = addressed_flags(markings)
    snap.observer_stats, boards = practice_stats(live)
    snap.boards = [b for b in boards if not is_test_county(b.county)]
    snap.test_boards = [b for b in boards if is_test_county(b.county)]
    snap.targeted = targeted_summaries(live, boards, snap.observer_stats, workspace)
    snap.public_targeted = [t for t in snap.targeted if not t.retired and not is_test_county(t.county)]
    snap.lookups = lookup_summaries(boards)
    snap.public_lookups = [x for x in snap.lookups if not is_test_county(x.county)]
    for event in live:
        if event.kind == "extract.built" and event.county and event.data.get("order") != "targeted":
            count = event.data.get("previously_targeted")
            snap.previously_targeted[event.county] = count if isinstance(count, int) else 0

    links = {county: n for county, n in active_links(live).items() if not is_test_county(county)}
    # A county with observer links or entries is listed even if nothing has arrived from it yet.
    observed = set(links) | {b.county for b in snap.boards}
    everything = upload_status(log, [c for c in (*counties, *sorted(observed)) if canonical_county(c)])
    statuses = {name: entry for name, entry in everything.items() if not is_test_county(name)}

    def county_row(name: str, entry: Any) -> CountyRow:
        row = CountyRow(
            county=name,
            audit_boards=entry.audit_boards,
            audit_boards_placeholder=entry.audit_boards_placeholder,
            rejected=entry.rejected,
            # The pull list arrives only after the drawing, so its absence is not shown as missing.
            missing=[m for m in entry.missing if m != "pull_list"],
        )
        if entry.last_rejection:
            row.last_rejection = str(entry.last_rejection.get("reason", "")).replace("_", " ")
        for kind, received in entry.latest.items():
            item: dict[str, Any] = {
                "name": received.original_name,
                "sha256": received.sha256,
                "size": received.size,
                "time": received.server_time,
                "actor": received.actor,
                "warnings": list(received.warnings),
            }
            if kind == "redacted_cvr":
                item["verdict"] = received.verdict
                item["counts"] = _counts(reports.get(received.sha256))
                row.cvr = item
            elif kind == "manifest":
                row.manifest = item
            elif kind == "pull_list":
                item.update(pull_lists.get(received.sha256, {}))
                row.pull_list = item
        return row

    snap.counties = [county_row(name, entry) for name, entry in statuses.items()]
    snap.test_counties = [county_row(name, entry) for name, entry in everything.items() if is_test_county(name)]

    if previous is not None and previous.datamap is not None and previous.map_seq == snap.map_seq:
        _apply_map(snap, previous.datamap, previous.flag_objects, list(previous.map_errors))
        snap.corla, snap.rounds = previous.corla, previous.rounds
    else:
        _build_map(snap, log, workspace, flag_checker)
    _apply_statuses(snap, statuses, links)
    _apply_view(snap, log, workspace, now)
    return snap


def _apply_view(snap: Snapshot, log: EventLog, workspace: Workspace, now: str | None) -> None:
    """Stage the problems as of the clock (`corla_observer.due`): what is due, and what is not yet expected."""
    from corla_observer.due import UPLOAD, stage, view_for

    datamap = snap.datamap
    if datamap is None:
        return
    participating = [name for name, st in snap.statuses.items() if st.participating]
    view = view_for(datamap, workspace, log, snap.raw_problems, now=now, followed=participating)
    public = stage(
        datamap, snap.raw_public_problems, now=view.now, now_source=view.now_source, calendar=view.calendar, followed=view.followed
    )
    snap.view = view
    snap.problems = [g.to_dict() for g in view.lines]
    snap.public_problems = [g.to_dict() for g in public.lines]
    snap.problem_count = len(view.current)
    snap.error_count = view.errors
    snap.public_error_count = public.errors
    snap.waiting = [w.to_dict() for w in view.waiting]
    snap.status_lines = list(view.status)
    snap.as_of, snap.as_of_source, snap.valid_until, snap.upcoming = view.now, view.now_source, view.valid_until, view.upcoming
    upload = view.upcoming.get(UPLOAD)
    if upload:
        for row in snap.counties:
            snap.not_yet[row.county] = {"cvr": upload, "corla": upload}
        for county in snap.statuses:
            snap.not_yet.setdefault(county, {"corla": upload})


def _build_map(snap: Snapshot, log: EventLog, workspace: Workspace, flag_checker: FlagChecker | None) -> None:
    checker = flag_checker if flag_checker is not None else check_flags
    try:
        datamap = build_map(log, workspace)
    except Exception as exc:  # show the rest of the dashboard regardless
        snap.errors.append(f"the data map could not be built: {type(exc).__name__}: {exc}"[:500])
        return
    errors: list[str] = []
    try:
        flags = checker(log, workspace, datamap)
    except Exception as exc:
        errors.append(f"the change flags could not be checked: {type(exc).__name__}: {exc}"[:500])
        flags = []
    _apply_map(snap, datamap, flags, errors)
    try:
        snap.corla = corla_uploads(datamap, workspace)
    except Exception as exc:
        snap.errors.append(f"CORLA's upload status could not be read: {type(exc).__name__}: {exc}"[:500])
    snap.rounds = audit_rounds(datamap)


def _apply_statuses(snap: Snapshot, ours: dict[str, CountyStatus], links: dict[str, int]) -> None:
    datamap = snap.datamap
    snap.statuses = county_statuses(
        ours,
        snap.corla,
        snap.rounds,
        seed_source=snap.seed_source if snap.seed else None,
        round_start=datamap.round_start if datamap is not None else None,
        boards=[b for b in snap.boards if b.list_key == RLA],
        links=links,
    )
    for row in snap.counties:
        row.status = snap.statuses.get(row.county)


def _apply_map(snap: Snapshot, datamap: DataMap, flags: list[Any], errors: list[str]) -> None:
    snap.datamap = datamap
    snap.map_errors = errors
    snap.errors.extend(errors)
    snap.election = datamap.election
    if datamap.seed is not None:
        snap.seed = str(datamap.seed.value)
        snap.seed_source = datamap.seed.source
    if datamap.roll_time is not None:
        snap.roll_time = str(datamap.roll_time.value)
    snap.manifests_in_map = len(datamap.manifests)
    snap.contests = len(datamap.contests)
    problems = datamap.check()
    snap.raw_problems = unflagged_problems(problems, flags)
    snap.raw_public_problems = [p for p in problems if p.code not in PROBLEM_FLAG_REASONS]
    by_county = {row.county: row for row in snap.counties}
    for county, chosen in datamap.manifests.items():
        if county in by_county:
            by_county[county].map_manifest = {
                "ref": short_ref(chosen.ref),
                "sha256": chosen.sha256,
                "batches": len(chosen.batches),
                "ballot_cards": chosen.ballot_cards,
            }
    snap.flag_objects = flags
    for flag in flags:
        d: dict[str, Any] = flag.to_dict()
        d["refs"] = [short_ref(str(r)) for r in flag.refs]
        marking, holds = addressing(flag, snap.addressed)
        d["addressed"] = marking.to_dict() if marking else None
        d["addressed_holds"] = holds
        d["reopened_by_new"] = marking is not None and not holds
        snap.flags.append(d)
        if holds:
            continue
        for county in flag.counties:
            if county in by_county:
                by_county[county].flags += 1


def map_text(snapshot: Snapshot, *, for_ai: bool) -> str:
    """The data map as `corla_observer.datamap.describe` prints it (``map show [--for-ai]``)."""
    if snapshot.datamap is None:
        return "\n".join(snapshot.errors) or "no data map"
    return describe(snapshot.datamap, for_ai=for_ai, flags=snapshot.flag_objects, view=snapshot.view)


class SnapshotCache:
    """The latest snapshot, rebuilt only when the log's head has moved."""

    def __init__(self, build: Callable[[Snapshot | None], Snapshot], head: Callable[[], tuple[int, str]]) -> None:
        self._build = build
        self._head = head
        self._snapshot: Snapshot | None = None
        self._lock = threading.Lock()

    def get(self) -> Snapshot:
        """The snapshot, rebuilt if the log's head has moved or a due time it was judged before has passed."""
        with self._lock:
            seq, digest = self._head()
            stale = (
                self._snapshot is not None
                and self._snapshot.valid_until is not None
                and self._snapshot.as_of_source == "now"
                and format_server_time(datetime.now(UTC)) >= self._snapshot.valid_until
            )
            if stale or self._snapshot is None or (self._snapshot.head_seq, self._snapshot.head_hash) != (seq, digest):
                self._snapshot = self._build(self._snapshot)
            return self._snapshot


def missing_label(kind: str) -> str:
    return {"redacted_cvr": "redacted CVR", "pull_list": "pull list", "audit_boards": "audit boards"}.get(kind, kind)
