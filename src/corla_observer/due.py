"""When each kind of input is due, and the data map's problems as of a moment: what is wrong now, and what is not yet expected.

The data map (`corla_observer.datamap`) reports everything missing for a
sampling run, whatever the time: just after the target list is posted, that
is a missing manifest for every county, a missing seed and an estimated
sample count for every contest. None of that is a problem yet. This module
puts each problem in its stage:

**Due times.** Each kind of input (`EXPECTED`) is due at a calendar
milestone (`Calendar`): the workspace's ``election.toml`` ``[calendar]``
(`corla_observer.election`), else the built-in calendar of a known election
(`KNOWN_CALENDARS`). The manifests, the counties' redacted CVRs and CORLA's
upload status are due by the upload deadline (``tabulation_deadline``: the
10th day after the election, 2:00 PM); the seed at the roll (``roll``, the
13th day); the pull lists and CORLA's contest list and selections are
expected after the roll; the target contest list by ``targets_deadline``.
Election night results are never due: they are only status.

**The clock.** In a walkthrough (`corla_observer.walkthrough`) the
simulated time of the last step applied (before the first, the first
step's); otherwise the real time (`clock`).

**Staging** (`stage`). Before its due time, a missing input is not a
problem: its problems (`Expected.codes`) are held back and it is listed in a
short "not yet expected" summary with counts (`Waiting`: "Manifests: 0 of
63 posted, due Fri 10 Jul 2:00 PM"). From its due time on, its problems are
problems as before, and an input that has none of its own in the data map
gets one (CORLA's upload status, a followed county's redacted CVR or pull
list, CORLA's selections). Anything present but wrong (a file that cannot
be read, a hash or batch mismatch, a manifest under another county's name)
is a problem whatever the time: only the codes of missing inputs are ever
held back.

**Grouping** (`group_problems`). When three or more counties have the same
problem, it is one line with the count and the county names
(`ProblemGroup`); the counties followed (the pilot counties, the
walkthrough's county, counties that uploaded files or take part in
observation) keep their own lines.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from corla_observer.classify import COLORADO_COUNTIES
from corla_observer.datamap import DataMap, Problem, parse_time
from corla_observer.events import EventLog, JSONValue, format_server_time
from corla_observer.localtime import colorado_time, parse_instant, to_colorado
from corla_observer.sampling import county_key
from corla_observer.workspace import Workspace

if TYPE_CHECKING:
    from corla_observer.election import ElectionSetup

#: The calendar milestones inputs are due at (`corla_observer.election`'s ``[calendar]`` keys).
TARGETS = "targets_deadline"
UPLOAD = "tabulation_deadline"
ROLL = "roll"

#: Colorado counties in CORLA's audit (San Juan counts by hand).
CORLA_COUNTIES = tuple(c for c in COLORADO_COUNTIES if c != "San Juan")

#: The Secretary of State's calendars of past and coming elections, as Colorado times.
KNOWN_CALENDARS: dict[str, dict[str, str]] = {
    # Primary of 30 June 2026: tabulation done by the 10th day, roll on the 13th.
    "2026/primary": {UPLOAD: "2026-07-10 14:00", ROLL: "2026-07-13 10:00"},
    # General of 3 November 2026 (the Secretary of State's calendar of 31 July 2026).
    "2026/general": {TARGETS: "2026-11-06 17:00", UPLOAD: "2026-11-13 14:00", ROLL: "2026-11-16 10:00"},
}

#: Problems of three or more counties are grouped into one line.
GROUP_MIN = 3


@dataclass(frozen=True)
class Calendar:
    """The due times (UTC) by milestone key, and where they come from."""

    times: Mapping[str, str]
    source: str

    def at(self, key: str) -> str | None:
        return self.times.get(key)


def calendar_for(election: str | None, setup: ElectionSetup | None = None) -> Calendar:
    """The calendar for ``election``: the setup's ``[calendar]`` if it is this election's, else the built-in one."""
    if setup is not None and setup.calendar and (election is None or setup.election == election):
        return Calendar({m.key: m.time for m in setup.calendar}, "the workspace's election.toml")
    known = KNOWN_CALENDARS.get(election or "")
    if known:
        return Calendar({k: parse_time(v) for k, v in known.items()}, f"the Secretary of State's calendar for {election}")
    return Calendar({}, "no calendar known")


def due_text(moment: str) -> str:
    """A due time as people read it: ``Fri 10 Jul 2:00 PM``."""
    instant = parse_instant(moment)
    if instant is None:
        return moment
    local = to_colorado(instant)
    hour = local.hour % 12 or 12
    return f"{local:%a} {local.day} {local:%b} {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


@dataclass(frozen=True)
class Expected:
    """One kind of input, the milestone it is due at, and the data map's problem codes for its absence."""

    key: str
    label: str
    milestone: str
    #: Expected some time after the milestone, rather than by it.
    after: bool = False
    codes: frozenset[str] = frozenset()


EXPECTED: tuple[Expected, ...] = (
    Expected("targets", "Target contest list", TARGETS, codes=frozenset({"no_contests"})),
    Expected("manifests", "Manifests", UPLOAD, codes=frozenset({"no_manifest", "statewide_counties_assumed"})),
    Expected("upload_status", "CORLA's upload status", UPLOAD),
    Expected("cvrs", "Redacted CVRs of the counties followed", UPLOAD),
    Expected("seed", "Seed", ROLL, codes=frozenset({"no_seed"})),
    Expected("contest_list", "CORLA's contest list", ROLL, after=True, codes=frozenset({"estimated_sample_count"})),
    Expected("selections", "CORLA's selections", ROLL, after=True),
    Expected("pull_lists", "Pull lists of the counties followed", ROLL, after=True),
)


@dataclass(frozen=True)
class Waiting:
    """An input not yet expected: how much of it has arrived, and when it is due."""

    key: str
    label: str
    status: str
    due: str
    #: When it is due, in words (``due Fri 10 Jul 2:00 PM``, ``expected after the roll, ...``).
    when: str

    @property
    def text(self) -> str:
        return f"{self.label}: {self.status}, {self.when}"

    def to_dict(self) -> dict[str, JSONValue]:
        return {"key": self.key, "label": self.label, "status": self.status, "due": self.due, "when": self.when, "text": self.text}


@dataclass
class ProblemGroup:
    """One line of the problem list: a single problem, or one problem shared by several counties."""

    severity: str
    code: str
    message: str
    hint: str
    problems: list[Problem]
    counties: list[str] = field(default_factory=list)

    @property
    def county(self) -> str | None:
        return self.problems[0].county if len(self.problems) == 1 else None

    @property
    def grouped(self) -> bool:
        return len(self.problems) > 1

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(r for p in self.problems for r in p.refs))

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "county": self.county,
            "refs": list(self.refs),
            "count": len(self.problems),
            "counties": list(self.counties),
            "members": [p.message for p in self.problems] if self.grouped else [],
        }


#: How a group of one code's problems is summed up.
GROUP_SUMMARIES = {
    "no_manifest": "no usable manifest",
    "estimated_sample_count": "the sample count will be estimated; CORLA's own count is published later",
    "no_redacted_cvr": "redacted CVR not provided",
    "no_pull_list": "pull list not yet received",
    "manifest_differs_from_import": "the manifest is not the file CORLA recorded importing",
    "conflicting_manifests": "manifests that list different batches",
    "no_county_id": "no county ID for a multi-county contest's domain",
    "domain_size_differs": "the manifests' ballot cards differ from CORLA's contest list",
    "no_sample_count": "no sample count and no margin to estimate one from",
}


def _group_message(code: str, problems: Sequence[Problem], counties: Sequence[str]) -> str:
    summary = GROUP_SUMMARIES.get(code, code.replace("_", " "))
    where = f"{len(counties)} counties" if len(problems) == len(counties) else f"{len(problems)}, in {len(counties)} counties"
    return f"{summary} ({where})"


def group_problems(problems: Iterable[Problem], keep: Iterable[str] = ()) -> list[ProblemGroup]:
    """The problems as lines: one per code shared by `GROUP_MIN` or more counties, except for the counties in ``keep``.

    The order is kept, each group taking its first problem's place.
    """
    kept = {county_key(c) for c in keep}
    items = list(problems)
    pools: dict[tuple[str, str], list[Problem]] = {}
    for problem in items:
        if problem.county is not None and county_key(problem.county) not in kept:
            pools.setdefault((problem.severity, problem.code), []).append(problem)
    grouped = {
        key: members for key, members in pools.items() if len({county_key(p.county or "") for p in members}) >= GROUP_MIN
    }
    lines: list[ProblemGroup] = []
    placed: set[tuple[str, str]] = set()
    for problem in items:
        key = (problem.severity, problem.code)
        if key in grouped and problem in grouped[key]:
            if key in placed:
                continue
            placed.add(key)
            members = grouped[key]
            counties = sorted({p.county or "" for p in members}, key=county_key)
            hints = [p.hint for p in members if p.hint]
            lines.append(
                ProblemGroup(problem.severity, problem.code, _group_message(problem.code, members, counties), hints[0] if hints else "", members, counties)
            )
        else:
            lines.append(ProblemGroup(problem.severity, problem.code, problem.message, problem.hint, [problem], [problem.county] if problem.county else []))
    return lines


@dataclass(frozen=True)
class Clock:
    """The time problems are judged at (UTC), and what it is."""

    now: str
    source: str


def clock(workspace: Workspace, log: EventLog | None = None, *, now: str | None = None) -> Clock:
    """The time to judge problems at: ``now`` if given, a walkthrough's simulated time, or the real time."""
    if now is not None:
        return Clock(now, "the time asked for")
    from corla_observer.walkthrough import WalkthroughError, is_walkthrough, state

    if log is not None and is_walkthrough(workspace):
        try:
            walk = state(workspace, log)
        except WalkthroughError:
            walk = None
        if walk is not None:
            if walk.now:
                return Clock(walk.now, "the walkthrough's simulated time")
            if walk.steps:
                return Clock(walk.steps[0].utc, "the walkthrough's start")
    return Clock(format_server_time(datetime.now(UTC)), "now")


def followed_counties(datamap: DataMap, workspace: Workspace, setup: ElectionSetup | None = None) -> list[str]:
    """The counties followed: the setup's pilot counties, a walkthrough's county, and every county with an upload."""
    found: list[str] = list(setup.pilot_counties) if setup else []
    from corla_observer.walkthrough import walkthrough_county

    county = walkthrough_county(workspace)
    if county:
        found.append(county)
    found.extend(e.county for e in datamap.entries if e.source == "upload" and e.county)
    return sorted(set(found), key=county_key)


@dataclass
class ProblemView:
    """The data map's problems as of a moment; see the module documentation."""

    #: The time judged at (UTC), and what it is; None judges everything as due.
    now: str | None
    now_source: str
    calendar: Calendar
    #: The problems now, and the ones held back until their input is due.
    current: list[Problem]
    held: list[Problem]
    waiting: list[Waiting]
    lines: list[ProblemGroup]
    #: Status only, never a problem (election night results).
    status: list[str] = field(default_factory=list)
    #: The next due time after ``now``, when the view changes by itself.
    valid_until: str | None = None
    #: Due texts of the milestones not yet reached, by key (``tabulation_deadline``: ``Fri 10 Jul 2:00 PM``).
    upcoming: dict[str, str] = field(default_factory=dict)
    #: The counties followed, whose problems are never grouped.
    followed: list[str] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(1 for p in self.current if p.severity == "error")

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "now": self.now,
            "now_source": self.now_source,
            "calendar": {"source": self.calendar.source, "times": dict(self.calendar.times)},
            "waiting": [w.to_dict() for w in self.waiting],
            "problems": [g.to_dict() for g in self.lines],
            "held": [p.to_dict() for p in self.held],
            "status": list(self.status),
        }


def _present(datamap: DataMap, kind: str) -> bool:
    return bool(datamap.used(kind))


def _uploaded(datamap: DataMap, kind: str) -> set[str]:
    return {county_key(e.county) for e in datamap.entries if e.source == "upload" and e.county and e.kind == kind and not e.ignored}


def _progress(item: Expected, datamap: DataMap, followed: Sequence[str]) -> tuple[bool, str, list[str]]:
    """Whether the input is complete, its status in words, and the followed counties still without it."""
    if item.key == "targets":
        done = _present(datamap, "target_contests") or _present(datamap, "contests_list")
        return done, "posted" if done else "not yet posted", []
    if item.key == "manifests":
        posted = sum(1 for c in CORLA_COUNTIES if c in datamap.manifests)
        return posted >= len(CORLA_COUNTIES), f"{posted} of {len(CORLA_COUNTIES)} posted", []
    if item.key == "upload_status":
        done = _present(datamap, "upload_status")
        return done, "posted" if done else "not yet posted", []
    if item.key in ("cvrs", "pull_lists"):
        received = _uploaded(datamap, "cvr" if item.key == "cvrs" else "pull_list")
        lacking = [c for c in followed if county_key(c) not in received]
        status = f"{len(followed) - len(lacking)} of {len(followed)} received"
        if lacking:
            status += f" (none yet from {', '.join(lacking)})"
        return not lacking, status, lacking
    if item.key == "seed":
        done = datamap.seed is not None
        return done, "entered" if done else "not yet entered", []
    if item.key == "contest_list":
        done = _present(datamap, "contests_list")
        estimated = sum(1 for c in datamap.contests if c.sample_count is None)
        status = "posted" if done else "not yet posted"
        if not done and estimated:
            status += f" ({estimated} sample counts estimated until then)"
        return done, status, []
    if item.key == "selections":
        done = _present(datamap, "selection_list")
        return done, "posted" if done else "not yet posted", []
    return True, "", []


def _overdue(item: Expected, due: str, lacking: Sequence[str]) -> list[Problem]:
    """The problems of an input past its due time that the data map has no problem of its own for."""
    when = due_text(due)
    if item.key == "upload_status":
        return [
            Problem(
                "warning",
                "no_upload_status",
                f"CORLA's upload status has not been posted; counties' uploads were due {when}",
                hint="fetch again; it shows which counties' manifests and CVRs CORLA imported",
            )
        ]
    if item.key == "cvrs":
        return [
            Problem(
                "warning",
                "no_redacted_cvr",
                f"{county}'s redacted CVR has not been provided; counties' CVRs were due in CORLA {when}",
                hint="ask the county for it, and upload it",
                county=county,
            )
            for county in lacking
        ]
    if item.key == "pull_lists":
        return [
            Problem("info", "no_pull_list", f"{county}'s pull list has not been received (expected after the roll, {when})", county=county)
            for county in lacking
        ]
    if item.key == "selections":
        return [Problem("info", "no_selections_yet", f"CORLA's selections have not been posted (expected after the roll, {when})")]
    return []


def stage(
    datamap: DataMap,
    problems: Iterable[Problem],
    *,
    now: str | None,
    calendar: Calendar,
    followed: Sequence[str] = (),
    now_source: str = "",
    results: str | None = None,
) -> ProblemView:
    """``problems`` (from `DataMap.check`, perhaps without those a flag reports) as of ``now``.

    With ``now`` None, every problem is current and nothing is held back.
    ``results`` is a status line about election night results, if any.
    """
    items = list(problems)
    status = [results] if results else []
    if now is None:
        lines = group_problems(items, followed)
        return ProblemView(None, now_source or "every input judged as due", calendar, items, [], [], lines, status, followed=list(followed))
    waiting: list[Waiting] = []
    held_codes: set[str] = set()
    extra: list[Problem] = []
    upcoming: dict[str, str] = {}
    for key in (TARGETS, UPLOAD, ROLL):
        moment = calendar.at(key)
        if moment and moment > now:
            upcoming[key] = due_text(moment)
    for item in EXPECTED:
        if item.key in ("cvrs", "pull_lists") and not followed:
            continue
        done, words, lacking = _progress(item, datamap, followed)
        due = calendar.at(item.milestone)
        if done:
            continue
        if due is not None and due > now:
            held_codes |= item.codes
            when = due_text(due)
            phrase = f"expected after the roll, {when}" if item.after else (f"due at the roll, {when}" if item.milestone == ROLL else f"due {when}")
            waiting.append(Waiting(item.key, item.label, words, due, phrase))
        elif due is not None:
            extra.extend(_overdue(item, due, lacking))
    current = [p for p in items if p.code not in held_codes]
    held = [p for p in items if p.code in held_codes]
    order = {"error": 0, "warning": 1}
    current = sorted(current + extra, key=lambda p: (order.get(p.severity, 2), p.code))
    future = sorted(t for t in calendar.times.values() if t > now)
    return ProblemView(
        now,
        now_source,
        calendar,
        current,
        held,
        waiting,
        group_problems(current, followed),
        status,
        future[0] if future else None,
        upcoming,
        list(followed),
    )


def results_status(log: EventLog) -> str | None:
    """A status line about the election night results recorded, or None if there are none."""
    count, latest = 0, None
    for event in log.iter_events():
        if event.kind in ("results.fetched", "results.uploaded"):
            count += 1
            simulated = event.data.get("simulated")
            moment = simulated.get("time") if isinstance(simulated, dict) else None
            latest = moment if isinstance(moment, str) else event.server_time
    if not count:
        return None
    return f"Election night results: {count} file{'s' if count != 1 else ''} recorded, the latest {colorado_time(latest, seconds=False)} (status only, never due)"


def view_for(
    datamap: DataMap,
    workspace: Workspace,
    log: EventLog | None,
    problems: Iterable[Problem] | None = None,
    *,
    now: str | None = None,
    setup: ElectionSetup | None = None,
    followed: Iterable[str] = (),
) -> ProblemView:
    """The view of ``problems`` (default: all of the map's) for a workspace: its setup's calendar, its clock, its counties followed."""
    if setup is None:
        from corla_observer.election import SetupError, load_setup

        try:
            setup = load_setup(workspace)
        except SetupError:
            setup = None
    when = clock(workspace, log, now=now)
    counties = sorted(set(followed_counties(datamap, workspace, setup)) | set(followed), key=county_key)
    return stage(
        datamap,
        datamap.check() if problems is None else problems,
        now=when.now,
        now_source=when.source,
        calendar=calendar_for(datamap.election, setup),
        followed=counties,
        results=results_status(log) if log is not None else None,
    )


def view_lines(view: ProblemView, *, hints: bool = True) -> list[str]:
    """The view as text: the "not yet expected" summary, the status lines and the problem lines."""
    out: list[str] = []
    if view.now is not None:
        out.append(f"as of {colorado_time(view.now, seconds=False)} ({view.now_source}; due times from {view.calendar.source})")
    if view.waiting:
        out.append("not yet expected:")
        out.extend(f"  {w.text}" for w in view.waiting)
    for line in view.status:
        out.append(line)
    out.append(f"problems ({len(view.lines)}):" if view.lines else "problems: none")
    for group in view.lines:
        refs = f" [{', '.join(_short_ref(r) for r in group.refs[:4])}]" if group.refs else ""
        where = f": {', '.join(group.counties)}" if group.grouped else ""
        out.append(f"  {group.severity.upper()} {group.code}: {group.message}{where}{refs if not group.grouped else ''}")
        if hints and group.hint:
            out.append(f"    hint: {group.hint}")
    return out


def _short_ref(ref: str) -> str:
    prefix = "https://www.coloradosos.gov/pubs/elections/RLA/"
    return "RLA/" + ref[len(prefix) :] if ref.startswith(prefix) else ref
