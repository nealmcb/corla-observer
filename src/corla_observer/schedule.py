"""Scheduled crawls: when to fetch the audit center and the results, and the long-running loop that does it.

**The cycle.** `run_cycle` is one complete update of a workspace, the same
whether the schedule, a coordinator's "fetch now" or a sprint starts it:

1. the election setup (`corla_observer.election`) is read and, if it
   changed, recorded;
2. the Audit Center is crawled from the setup's page, with its probe URLs
   (`corla_observer.fetch.crawl`, at most 10 requests a second, slowing
   down when the server pushes back), which classifies every new or
   changed file; then, if ``schedule.toml`` names the server's clone of
   the public mirror (``mirror_clone``), the files whose content changed
   are committed there (`corla_observer.mirror`, docs/MIRROR.md), and
   published at once in a window with ``publish_mirror = true``. A
   failure there is recorded and shown, and never fails the cycle;
3. if the window asks for it, the election night results are gathered
   (`corla_observer.enr.gather`: one small request, more only when they
   changed);
4. the data map is built, the totals check is recorded when its inputs
   changed (`corla_observer.totals`: the results against the other results
   files, the redacted CVRs, CORLA's tallies and the manifests), and the
   change flags not yet recorded are recorded (`corla_observer.flags`);
5. the estimates are made and recorded when they changed
   (`corla_observer.estimate`);
6. once a seed is in effect, the selections are predicted again when the
   prediction would change (new counts, manifests or estimates), and every
   county's interim extract (predicted, or from CORLA's posting) is rebuilt
   when it would change; an extract from the county's pull list is left to
   the coordinators;
7. a ``schedule.cycle`` event records what the cycle did.

A results file uploaded by hand runs steps 4 to 6 at once
(`update_after_upload`).

A cycle takes the workspace's ``cycle.lock`` (an exclusive ``flock``), so
the scheduler, the web server and a sprint never run two at once; a cycle
that finds it held is skipped.

**The schedule.** The workspace's ``schedule.toml`` (`SCHEDULE_FILENAME`),
which a coordinator may edit at any time (it is read again before every
decision), lists windows, each with a start, an end (Colorado time), an
interval, whether to gather results and whether to publish the mirror's
waiting commits after each crawl (``publish_mirror``, false by default);
at its top, ``mirror_clone`` names the server's clone of the public mirror
(none by default: no mirror update). Inside a window a cycle is due
when the last cycle started at least the interval ago; a cycle missed while
the machine slept is made once, on waking. `GENERAL_2026_SCHEDULE` is the
2026 general election's schedule (`init_schedule` writes it):

- Fri 6 Nov, the target-list deadline (5 PM MT): one crawl at 7 PM;
- Sat 7 to Thu 12 Nov: once a day at noon, for early postings;
- Thu 12 Nov 6 PM to Fri 13 Nov midnight: hourly;
- the weekend, to Mon 16 Nov 6 AM: every 8 hours;
- Mon 16 Nov, roll day, 6 AM to 8 PM, without results: every 20 minutes, but every 10 minutes from
  9 AM to noon, around the seed meeting at 10;
- Tue 17 to Wed 25 Nov: hourly, for the rounds' files.

Before the first window the loop does nothing but sleep, so it is safe to
leave running.

**Sprints.** `request_sprint` records a ``schedule.sprint_requested``
event (until when, and how often); the loop then runs a cycle at that
interval (at least `MIN_SPRINT_SECONDS`) until the time given, whatever
the schedule says; ``schedule.sprint_stopped`` ends it early. `sprint`
runs one in the foreground instead, for when no loop is running.

**The loop's own events.** Besides each cycle, the loop records what it
does, each when it changes rather than at every look: ``schedule.started``
when it starts (its software, compared with its last start, as the web
server's ``server.started``: `corla_observer.version.record_start`);
``schedule.state`` when what it is doing changes (in a window, a sprint,
between windows, no schedule file: ``state``, ``reason``, ``window``,
``sprint_until``); ``schedule.file_error`` when ``schedule.toml`` cannot be
read, and again (``error`` null) once it can; ``schedule.awake`` when it
takes or releases the sprite's keep-awake hold (``held``); and
``schedule.stopped`` when it ends (``reason``: stopped, a signal, or the
error that ended it). An unexpected ``schedule.started`` with no
``schedule.stopped`` before it means the scheduler was killed or the
machine went down.

**Heartbeat.** The loop writes the workspace's ``scheduler.heartbeat``
(`HEARTBEAT_FILENAME`: its process ID and the time) each time it looks,
at least every `MAX_SLEEP_SECONDS` while idle, so the web pages can tell
whether a scheduler is running for the workspace (`scheduler_seen`): a
sprint requested where none runs would do nothing.

**Keeping a sprite awake.** On a Fly.io sprite, which pauses when idle,
the loop holds a Sprites "task" (``/.sprite/api.sock``, refreshed every
minute with a 5-minute expiry) while ``keep_awake`` (in the schedule file)
says to: ``always`` (the default: from the loop's start), ``windows``
(only within an hour of a window or during a sprint; a paused sprite then
wakes only on an incoming request), or ``never``.

Events: ``schedule.cycle`` (``trigger``, ``started``, ``window``, ``crawl`` counts,
``crawl_incomplete``, ``outside_links`` (first seen in this crawl), ``mirror`` (the
mirror update's summary, `corla_observer.mirror.after_crawl`, if it ran),
``results`` (changed, version), ``flags_recorded``, ``estimate_seq``,
``prediction_seq``, ``extracts`` (county to seq), ``errors``,
``seconds``), ``schedule.cycle_skipped`` (``reason``),
``schedule.sprint_requested`` (``until``, ``every_seconds``),
``schedule.started``, ``schedule.state``, ``schedule.file_error``,
``schedule.awake``, ``schedule.stopped`` (above),
``schedule.sprint_stopped``.

Standard library only.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import socket
import time
import tomllib
import traceback
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from corla_observer.datamap import build_map, parse_time
from corla_observer.election import ElectionSetup, SetupError, load_setup, record_setup
from corla_observer.events import EventLog, JSONValue, check_actor, format_server_time
from corla_observer.localtime import colorado_time
from corla_observer.workspace import Workspace

SCHEDULE_FILENAME = "schedule.toml"
LOCK_FILENAME = "cycle.lock"
SCHEDULE_FORMAT = 1
ACTOR = "system"
#: Shortest interval a sprint may ask for.
MIN_SPRINT_SECONDS = 30
#: Longest the loop sleeps before looking again (so edits and sprint requests are seen soon).
MAX_SLEEP_SECONDS = 20.0
#: The loop's heartbeat file in the workspace.
HEARTBEAT_FILENAME = "scheduler.heartbeat"
#: A heartbeat at most this old means a scheduler is running (a cycle may take a few minutes without one).
HEARTBEAT_FRESH_SECONDS = 600
SPRITE_SOCKET = "/.sprite/api.sock"
SPRITE_TASK = "corla-observer-schedule"
KEEP_AWAKE = ("always", "windows", "never")

GENERAL_2026_SCHEDULE = """\
# Crawl schedule for corla-observer, 2026 general election. Times are Colorado time.
# Edit freely: the scheduler reads this file again before every decision.
format = 1
# always: keep the sprite awake from the scheduler's start; windows: only near
# a window or during a sprint (a paused sprite then wakes only on a request);
# never.
keep_awake = "always"
# The server's clone of the public mirror (github.com/nealmcb/auditcenter):
# after every crawl the files whose content changed are committed there, to
# be published from /coord, or at once in a window with publish_mirror = true
# (docs/MIRROR.md). Leave it out to keep no mirror.
# mirror_clone = "~/auditcenter"

[[window]]
name = "target list"        # deadline to select the targeted contests: Fri 6 Nov 5 PM; one crawl at 7 PM
start = "2026-11-06 19:00"
end = "2026-11-06 23:59"
every = "6h"
results = true

[[window]]
name = "early postings"
start = "2026-11-07 12:00"
end = "2026-11-12 12:30"
every = "24h"
results = true

[[window]]
name = "tabulation deadline"  # counties' hashed manifests and CVRs due in CORLA Fri 13 Nov 2 PM
start = "2026-11-12 18:00"
end = "2026-11-13 23:59"
every = "1h"
results = true

[[window]]
name = "weekend"
start = "2026-11-14 00:00"
end = "2026-11-16 06:00"
every = "8h"
results = true

[[window]]
name = "roll day"             # seed rolled Mon 16 Nov at 10 AM
start = "2026-11-16 06:00"
end = "2026-11-16 09:00"
every = "20m"
results = false
publish_mirror = true

[[window]]
name = "roll day, seed meeting"  # the seed meeting at 10, and round 1's files posted after it
start = "2026-11-16 09:00"
end = "2026-11-16 12:00"
every = "10m"
results = false
publish_mirror = true

[[window]]
name = "roll day, afternoon"
start = "2026-11-16 12:00"
end = "2026-11-16 20:00"
every = "20m"
results = false
publish_mirror = true

[[window]]
name = "audit"
start = "2026-11-17 00:00"
end = "2026-11-25 23:59"
every = "1h"
results = false
"""

DEFAULT_SCHEDULES = {"2026/general": GENERAL_2026_SCHEDULE}


class ScheduleError(ValueError):
    """A schedule file does not check out; ``errors`` lists every problem."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Window:
    name: str
    start: str
    end: str
    every_seconds: int
    results: bool
    #: Whether the mirror's waiting commits are published after each crawl in this window.
    publish_mirror: bool = False


@dataclass(frozen=True)
class Schedule:
    windows: tuple[Window, ...]
    keep_awake: str = "always"
    sha256: str | None = None
    #: The server's clone of the public mirror, as written (``~`` allowed; relative to the workspace), or None.
    mirror_clone: str | None = None

    def window_at(self, moment: str) -> Window | None:
        """The window ``moment`` (UTC, the event log's format) falls in; the latest-starting one if several."""
        inside = [w for w in self.windows if w.start <= moment <= w.end]
        return max(inside, key=lambda w: w.start) if inside else None

    def next_start(self, moment: str) -> str | None:
        """The start of the next window after ``moment``, or None."""
        later = [w.start for w in self.windows if w.start > moment]
        return min(later) if later else None


def parse_interval(text: str) -> int:
    """Seconds in an interval written ``30s``, ``3m``, ``1h``, ``1h30m`` or ``2d``."""
    total, number = 0, ""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for ch in text.strip().lower():
        if ch.isdigit():
            number += ch
        elif ch in units and number:
            total += int(number) * units[ch]
            number = ""
        elif ch != " ":
            raise ValueError(f"not an interval: {text!r}")
    if number or total <= 0:
        raise ValueError(f"not an interval: {text!r} (write 30s, 3m, 1h or 2d)")
    return total


def parse_schedule(text: str) -> Schedule:
    """Read and check a schedule file.

    Raises:
        ScheduleError: listing every problem.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ScheduleError([f"not valid TOML: {exc}"]) from None
    errors: list[str] = []
    if data.get("format") != SCHEDULE_FORMAT:
        errors.append(f"format must be {SCHEDULE_FORMAT}")
    keep = data.get("keep_awake", "always")
    if keep not in KEEP_AWAKE:
        errors.append(f"keep_awake must be one of {', '.join(KEEP_AWAKE)}")
    clone = data.get("mirror_clone")
    if clone is not None and (not isinstance(clone, str) or not clone.strip()):
        errors.append("mirror_clone must be the path of the server's clone of the mirror")
        clone = None
    windows = []
    for n, raw in enumerate(data.get("window", []), start=1):
        if not isinstance(raw, dict):
            errors.append(f"window {n} must be a table")
            continue
        try:
            start, end = parse_time(str(raw["start"])), parse_time(str(raw["end"]))
            every = parse_interval(str(raw["every"]))
        except (KeyError, ValueError) as exc:
            errors.append(f"window {n} ({raw.get('name', '')}): {exc}")
            continue
        if end <= start:
            errors.append(f"window {n} ({raw.get('name', '')}): ends before it starts")
            continue
        if every < MIN_SPRINT_SECONDS:
            errors.append(f"window {n}: every must be at least {MIN_SPRINT_SECONDS}s")
            continue
        publish = raw.get("publish_mirror", False)
        if not isinstance(publish, bool):
            errors.append(f"window {n}: publish_mirror must be true or false")
            continue
        windows.append(Window(str(raw.get("name") or f"window {n}"), start, end, every, bool(raw.get("results", True)), publish))
    if errors:
        raise ScheduleError(errors)
    return Schedule(tuple(windows), str(keep), hashlib.sha256(text.encode("utf-8")).hexdigest(), clone)


def load_schedule(workspace: Workspace) -> Schedule | None:
    """The workspace's schedule, or None if it has no schedule file."""
    path = workspace.root / SCHEDULE_FILENAME
    return parse_schedule(path.read_text(encoding="utf-8")) if path.is_file() else None


def init_schedule(workspace: Workspace, election: str, *, force: bool = False) -> Path:
    """Write the default schedule for ``election``; never over an existing file unless ``force``."""
    text = DEFAULT_SCHEDULES.get(election)
    if text is None:
        raise ScheduleError([f"no default schedule for {election!r}"])
    path = workspace.root / SCHEDULE_FILENAME
    if path.exists() and not force:
        raise ScheduleError([f"{SCHEDULE_FILENAME} exists already"])
    parse_schedule(text)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# One cycle


@contextlib.contextmanager
def cycle_lock(workspace: Workspace) -> Iterator[bool]:
    """Hold the workspace's cycle lock if it is free; yields whether it was taken."""
    path = workspace.root / LOCK_FILENAME
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass
class CycleReport:
    """What `run_cycle` did."""

    ran: bool = True
    crawl: dict[str, int] = field(default_factory=dict)
    #: Whether the crawl reached its cap of addresses (`corla_observer.fetch.CrawlReport.truncated`).
    crawl_incomplete: bool = False
    #: Links outside the RLA directory first seen in this crawl.
    outside_links: list[str] = field(default_factory=list)
    #: The mirror update's summary (`corla_observer.mirror.after_crawl`), if one ran.
    mirror: dict[str, Any] | None = None
    results_changed: bool | None = None
    flags_recorded: int = 0
    totals_seq: int | None = None
    estimate_seq: int | None = None
    prediction_seq: int | None = None
    extracts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0


CrawlFunction = Callable[[Workspace, EventLog, ElectionSetup | None], Any]
ResultsFunction = Callable[[Workspace, EventLog, ElectionSetup, list[str]], Any]


def default_crawl(workspace: Workspace, log: EventLog, setup: ElectionSetup | None) -> Any:
    """Crawl the Audit Center from the setup's page with its probe URLs (or the default page).

    For an election replayed from the public mirror (``[audit_center_mirror]``),
    apply its next update instead (`corla_observer.rehearse.mirror_update`);
    otherwise nothing if the setup says ``crawl = false``.
    """
    from corla_observer.fetch import DEFAULT_START_URL, crawl

    if setup is None:
        return crawl(DEFAULT_START_URL, workspace=workspace, log=log)
    if setup.mirror is not None:
        from corla_observer.rehearse import mirror_update

        return mirror_update(workspace, log, setup.mirror)
    if not setup.crawl:
        return None
    return crawl(setup.audit_center, workspace=workspace, log=log, extra_urls=setup.probe_urls)


def default_results(workspace: Workspace, log: EventLog, setup: ElectionSetup, counties: list[str]) -> Any:
    """Gather the election night results (`corla_observer.enr.gather`)."""
    from corla_observer.enr import gather

    return gather(workspace, log, setup, counties=counties)


def _prediction_key(prediction: Any) -> str:
    data = prediction.to_dict()
    for contest in data["contests"]:
        contest.pop("count_source", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _latest_prediction_key(workspace: Workspace, log: EventLog) -> str | None:
    from corla_observer.predict import latest_prediction

    latest = latest_prediction(workspace, log)
    if latest is None:
        return None
    data = dict(latest[1])
    contests = data.get("contests")
    for contest in contests if isinstance(contests, list) else []:
        if isinstance(contest, dict):
            contest.pop("count_source", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def update_derived(
    workspace: Workspace,
    log: EventLog,
    setup: ElectionSetup | None,
    report: CycleReport,
    actor: str = ACTOR,
    counties: Sequence[str] = (),
) -> None:
    """Steps 4 to 6 of a cycle: the totals check (`corla_observer.totals`), flags, estimates, and (with a seed) the prediction and interim extracts.

    The prediction covers the statewide contests and those of the setup's
    pilot counties, ``counties`` and every county with an extract.
    """
    from corla_observer.enr import load_results
    from corla_observer.estimate import estimate_contests, record_estimates
    from corla_observer.extract import ExtractError, build_extract, latest_extract_events
    from corla_observer.flags import check_flags, record_flags
    from corla_observer.predict import PredictionError, predict, record_prediction

    from corla_observer.totals import record_totals_check

    datamap = build_map(log, workspace)
    try:
        report.totals_seq = record_totals_check(workspace, log, datamap, setup, actor=actor)
    except Exception as exc:  # noqa: BLE001 - a cycle reports every failure and carries on
        report.errors.append(f"totals check: {exc}")
    try:
        report.flags_recorded = len(record_flags(log, check_flags(log, workspace, datamap), actor))
    except Exception as exc:  # noqa: BLE001 - a cycle reports every failure and carries on
        report.errors.append(f"flags: {exc}")
    results = load_results(workspace, log)
    estimates = estimate_contests(datamap, setup=setup, results=results, log=log)
    report.estimate_seq = record_estimates(log, estimates, setup=setup, results=results, actor=actor)
    if datamap.seed is None:
        return
    pilots = list(setup.pilot_counties) if setup else []
    from corla_observer.classify import is_test_county

    wanted = sorted(c for c in set(pilots) | set(counties) | set(latest_extract_events(log)) if not is_test_county(c))
    try:
        by_name = {e.name: e for e in estimates}
        prediction = predict(datamap, counties=wanted, estimates=by_name) if wanted else predict(datamap, estimates=by_name)
    except PredictionError as exc:
        report.errors.append(f"prediction: {exc}")
        return
    if _prediction_key(prediction) != _latest_prediction_key(workspace, log):
        report.prediction_seq, _ = record_prediction(workspace, log, prediction, actor)
    for county, event in sorted(latest_extract_events(log).items()):
        if event.data.get("order") not in ("predicted", "audit_center") or is_test_county(county):
            continue
        try:
            built = build_extract(workspace, log, county, actor=actor, skip_if_unchanged=True)
        except ExtractError as exc:
            report.errors.append(f"extract {county}: {exc}")
            continue
        if built.seq != event.seq:
            report.extracts[county] = built.seq


def _derive(workspace: Workspace, log: EventLog, setup: ElectionSetup | None, actor: str) -> list[str]:
    report = CycleReport()
    update_derived(workspace, log, setup, report, actor)
    notes = [
        ("totals checked" if report.totals_seq else "totals unchanged")
        + f"; {report.flags_recorded} new flag{'s' if report.flags_recorded != 1 else ''}"
        + ("; estimates recorded" if report.estimate_seq else "; estimates unchanged")
        + ("; prediction recorded" if report.prediction_seq else "")
    ]
    notes.extend(f"error: {e}" for e in report.errors)
    return notes


def update_after_upload(workspace: Workspace, log: EventLog, actor: str = ACTOR) -> list[str]:
    """After a results file is uploaded by hand, or the seed entered on the web: the totals check, flags,
    estimates and (with a seed) the prediction at once (`update_derived`), under the cycle lock.

    Returns notes for the coordinator: what was recorded, or that a cycle
    is running (the next one picks the change up).
    """
    with cycle_lock(workspace) as taken:
        if not taken:
            return ["a cycle is running: the next cycle checks the totals and updates the estimates"]
        setup = None
        notes = []
        try:
            setup = load_setup(workspace)
        except SetupError as exc:
            notes.append(f"election setup: {exc}")
        return notes + _derive(workspace, log, setup, actor)


def run_cycle(
    workspace: Workspace,
    log: EventLog,
    *,
    trigger: str,
    window: Window | None = None,
    results: bool = True,
    crawl_fn: CrawlFunction = default_crawl,
    results_fn: ResultsFunction = default_results,
    actor: str = ACTOR,
    started: str | None = None,
) -> CycleReport:
    """One update of the workspace; see the module documentation. Skipped (``ran`` False) if another is running.

    ``started`` is when the cycle began (UTC; now by default), recorded in
    its event for the scheduler's next decision.
    """
    check_actor(actor)
    report = CycleReport()
    started = started or format_server_time(datetime.now(UTC))
    began = time.monotonic()
    with cycle_lock(workspace) as taken:
        if not taken:
            log.append("schedule.cycle_skipped", actor, data={"trigger": trigger, "reason": "another cycle is running"})
            report.ran = False
            return report
        setup = None
        try:
            setup = load_setup(workspace)
            if setup is not None:
                record_setup(workspace, log, setup, actor)
        except SetupError as exc:
            report.errors.append(f"election setup: {exc}")
        crawled = None
        try:
            crawled = crawl_fn(workspace, log, setup)
            count = getattr(crawled, "count", None)
            if callable(count):
                report.crawl = {o: count(o) for o in ("new", "changed", "unchanged", "missing", "blocked")}
            report.crawl_incomplete = bool(getattr(crawled, "truncated", False))
            outside = getattr(crawled, "outside_links", None)
            report.outside_links = list(outside)[:50] if isinstance(outside, dict) else []
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"crawl: {exc}")
        # A past election replayed from the public mirror, or one never crawled, writes nothing to the mirror.
        if crawled is not None and (setup is None or (setup.mirror is None and setup.crawl)):
            from corla_observer.mirror import after_crawl

            # Never raises: a failure is recorded (``mirror.error``) and shown, and the cycle goes on.
            report.mirror = after_crawl(workspace, log, window=window, moment=started, actor=actor)
        if results and setup is not None:
            try:
                from corla_observer.enr import results_counties

                gathered = results_fn(workspace, log, setup, results_counties(build_map(log, workspace), setup))
                report.results_changed = bool(getattr(gathered, "changed", False))
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"results: {exc}")
        try:
            update_derived(workspace, log, setup, report, actor)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"update: {exc}: {traceback.format_exc(limit=3)[-400:]}")
        report.seconds = round(time.monotonic() - began, 1)
        data: dict[str, JSONValue] = {
            "trigger": trigger,
            "started": started,
            "window": window.name if window else None,
            "crawl": dict(report.crawl),
            "crawl_incomplete": report.crawl_incomplete,
            "outside_links": list(report.outside_links[:20]),
            "results_changed": report.results_changed,
            "flags_recorded": report.flags_recorded,
            "totals_seq": report.totals_seq,
            "estimate_seq": report.estimate_seq,
            "prediction_seq": report.prediction_seq,
            "extracts": dict(report.extracts),
            "errors": [e[:500] for e in report.errors[:20]],
            "seconds": int(report.seconds),
        }
        if report.mirror is not None:
            data["mirror"] = report.mirror
        log.append("schedule.cycle", actor, data=data)
    return report


# ---------------------------------------------------------------------------
# Sprints


def request_sprint(log: EventLog, actor: str, *, minutes: int, every_seconds: int, now: datetime | None = None) -> int:
    """Ask the scheduler to run a cycle every ``every_seconds`` for ``minutes``; return the event's seq."""
    check_actor(actor)
    if not 1 <= minutes <= 24 * 60:
        raise ValueError("a sprint lasts 1 minute to 24 hours")
    every = max(MIN_SPRINT_SECONDS, every_seconds)
    until = format_server_time((now or datetime.now(UTC)) + timedelta(minutes=minutes))
    return log.append("schedule.sprint_requested", actor, data={"until": until, "every_seconds": every}).seq


def stop_sprint(log: EventLog, actor: str) -> int:
    check_actor(actor)
    return log.append("schedule.sprint_stopped", actor, data={}).seq


@dataclass(frozen=True)
class Sprint:
    until: str
    every_seconds: int
    actor: str
    seq: int


def active_sprint(log: EventLog, now: str) -> Sprint | None:
    """The sprint in force at ``now`` (UTC), if any: the latest request not stopped and not over."""
    current: Sprint | None = None
    for event in log.iter_events():
        if event.kind == "schedule.sprint_requested":
            current = Sprint(str(event.data.get("until")), int(str(event.data.get("every_seconds") or MIN_SPRINT_SECONDS)), event.actor, event.seq)
        elif event.kind == "schedule.sprint_stopped":
            current = None
    return current if current is not None and current.until > now else None


def last_cycle_start(log: EventLog) -> str | None:
    """When the last cycle started (its event's time minus its duration), or None."""
    found = None
    for event in log.iter_events():
        if event.kind == "schedule.cycle" and isinstance(event.data.get("started"), str):
            found = str(event.data["started"])
        elif event.kind == "schedule.cycle":
            seconds = event.data.get("seconds")
            moment = datetime.strptime(event.server_time, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
            if isinstance(seconds, int):
                moment -= timedelta(seconds=seconds)
            found = format_server_time(moment)
    return found


# ---------------------------------------------------------------------------
# Deciding and looping


@dataclass(frozen=True)
class Decision:
    """Whether a cycle is due now, why, and when to look again."""

    due: bool
    reason: str
    window: Window | None = None
    results: bool = True
    wait_seconds: float = MAX_SLEEP_SECONDS


def _seconds_between(earlier: str, later: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    return (datetime.strptime(later, fmt) - datetime.strptime(earlier, fmt)).total_seconds()


def decide(schedule: Schedule | None, sprint: Sprint | None, last_start: str | None, now: str) -> Decision:
    """Whether a cycle is due at ``now``, given the schedule, any sprint and when the last cycle started."""
    if sprint is not None:
        if last_start is None or _seconds_between(last_start, now) >= sprint.every_seconds:
            return Decision(True, f"sprint requested by {sprint.actor} (every {sprint.every_seconds}s)", None, False)
        return Decision(False, "sprint", wait_seconds=min(MAX_SLEEP_SECONDS, sprint.every_seconds - _seconds_between(last_start, now)))
    if schedule is None:
        return Decision(False, "no schedule file")
    window = schedule.window_at(now)
    if window is None:
        nxt = schedule.next_start(now)
        return Decision(False, f"between windows; next starts {colorado_time(nxt, seconds=False)}" if nxt else "after the last window")
    if last_start is None or last_start < window.start or _seconds_between(last_start, now) >= window.every_seconds:
        return Decision(True, f"window {window.name!r} (every {window.every_seconds}s)", window, window.results)
    remaining = window.every_seconds - _seconds_between(last_start, now)
    return Decision(False, f"window {window.name!r}", window, window.results, min(MAX_SLEEP_SECONDS, max(1.0, remaining)))


def next_check(schedule: Schedule | None, sprint: Sprint | None, last_start: str | None, now: str) -> tuple[str | None, str]:
    """When the scheduler next checks the Audit Center (UTC; None if not known) and why: in a sprint or a window, one
    interval after the last cycle's start (now, if due); between windows, the next window's start."""
    decision = decide(schedule, sprint, last_start, now)
    if decision.due:
        return now, decision.reason
    if sprint is not None:
        when = _plus(last_start or now, sprint.every_seconds)
        return when, f"sprint, every {sprint.every_seconds} s"
    if schedule is None:
        return None, "no schedule file"
    if decision.window is not None:
        when = _plus(last_start or now, decision.window.every_seconds)
        return when, f"window {decision.window.name!r}, every {_every(decision.window.every_seconds)}"
    upcoming = schedule.next_start(now)
    return (upcoming, "the next window") if upcoming else (None, "after the last window")


def _plus(moment: str, seconds: float) -> str:
    return format_server_time(datetime.strptime(moment, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC) + timedelta(seconds=seconds))


def _every(seconds: int) -> str:
    return f"{seconds // 3600} h" if seconds % 3600 == 0 else (f"{seconds // 60} min" if seconds % 60 == 0 else f"{seconds} s")


def next_check_text(workspace: Workspace, log: EventLog, now: str) -> str:
    """The scheduler's next check of the Audit Center, in words, for the pages' live status: whether a
    scheduler is running for the workspace (its heartbeat), and when it next checks, in Colorado time."""
    if not scheduler_running(workspace, now):
        return "Audit Center checks: no scheduler running here"
    try:
        schedule = load_schedule(workspace)
    except ScheduleError:
        return "Audit Center checks: schedule.toml cannot be read"
    when, why = next_check(schedule, active_sprint(log, now), last_cycle_start(log), now)
    if when is None:
        return f"Audit Center checks: none scheduled ({why})"
    return f"next Audit Center check {colorado_time(when, seconds=False)} ({why})"


class SpriteKeepAwake:
    """Holds a Sprites task through the sprite's local API socket while asked to, refreshing it every minute."""

    def __init__(self, socket_path: str = SPRITE_SOCKET, name: str = SPRITE_TASK) -> None:
        self.socket_path = socket_path
        self.name = name
        self.last: float | None = None
        self.held = False

    @property
    def available(self) -> bool:
        return os.path.exists(self.socket_path)

    def _request(self, method: str, body: bytes = b"") -> int:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(5)
            conn.connect(self.socket_path)
            head = (
                f"{method} /v1/tasks/{self.name} HTTP/1.1\r\nHost: sprite\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            )
            conn.sendall(head.encode() + body)
            reply = conn.recv(256).decode("latin-1")
        parts = reply.split(" ", 2)
        return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    def hold(self, wanted: bool) -> None:
        """Refresh the hold (at most once a minute) if ``wanted``, else release it."""
        if not self.available:
            return
        try:
            if wanted and (self.last is None or time.monotonic() - self.last >= 60):
                self._request("PUT", b'{"expire": "5m"}')
                self.last = time.monotonic()
                self.held = True
            elif not wanted and self.held:
                self._request("DELETE")
                self.held = False
                self.last = None
        except OSError:
            pass


def _wants_awake(schedule: Schedule | None, sprint: Sprint | None, now: str) -> bool:
    keep = schedule.keep_awake if schedule else "always"
    if keep == "never":
        return False
    if keep == "always" or sprint is not None:
        return True
    assert schedule is not None
    soon = format_server_time(datetime.strptime(now, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC) + timedelta(hours=1))
    return schedule.window_at(now) is not None or (schedule.next_start(now) or "~") <= soon


def beat(workspace: Workspace, now: str) -> None:
    """Write the loop's heartbeat (its process ID and ``now``), replacing the last."""
    path = workspace.root / HEARTBEAT_FILENAME
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": os.getpid(), "time": now}) + "\n", encoding="utf-8")
    temporary.replace(path)


def scheduler_seen(workspace: Workspace) -> str | None:
    """When a scheduler loop last looked at this workspace (UTC, as recorded), or None if never."""
    try:
        found = json.loads((workspace.root / HEARTBEAT_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    moment = found.get("time") if isinstance(found, dict) else None
    return moment if isinstance(moment, str) else None


def scheduler_running(workspace: Workspace, now: str) -> bool:
    """Whether a scheduler loop looked at this workspace within `HEARTBEAT_FRESH_SECONDS` of ``now``."""
    seen = scheduler_seen(workspace)
    return seen is not None and _seconds_between(seen, now) <= HEARTBEAT_FRESH_SECONDS


def run_loop(
    workspace_dir: Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    stop: Callable[[], bool] = lambda: False,
    crawl_fn: CrawlFunction = default_crawl,
    results_fn: ResultsFunction = default_results,
    keep_awake: SpriteKeepAwake | None = None,
    echo: Callable[[str], None] = print,
) -> None:
    """The scheduler: decide, run a cycle when due, sleep, repeat, until ``stop()``; see the module documentation.

    Its start, its changes of state, schedule file errors, the keep-awake hold and its end are
    recorded as events ("The loop's own events"); an exception that ends it is recorded, then raised.
    """
    from corla_observer.version import record_start

    workspace = Workspace.open(workspace_dir)
    awake = keep_awake or SpriteKeepAwake()
    with EventLog.open(workspace_dir) as log:
        record_start(log, actor=ACTOR, kind="schedule.started", extra={"pid": os.getpid()})
    last_reason = None
    last_state: str | None = None
    last_error: str | None = None
    last_held = awake.held
    ending = "stopped"
    try:
        while not stop():
            now = format_server_time(clock())
            beat(workspace, now)
            try:
                schedule = load_schedule(workspace)
                error = None
            except ScheduleError as exc:
                schedule, error = None, str(exc)
            with EventLog.open(workspace_dir) as log:
                sprint = active_sprint(log, now)
                decision = decide(schedule, sprint, last_cycle_start(log), now)
                awake.hold(_wants_awake(schedule, sprint, now))
                if awake.available and awake.held != last_held:
                    log.append("schedule.awake", ACTOR, data={"held": awake.held})
                    last_held = awake.held
                if error != last_error:
                    log.append("schedule.file_error", ACTOR, data={"error": error[:500] if error else None})
                    last_error = error
                if error and error != last_reason:
                    echo(f"schedule.toml: {error}")
                    last_reason = error
                state = _state(schedule, sprint, decision)
                if state != last_state:
                    log.append("schedule.state", ACTOR, data={
                        "state": state, "reason": decision.reason, "window": decision.window.name if decision.window else None,
                        "sprint_until": sprint.until if sprint else None,
                    })  # fmt: skip
                    last_state = state
                if decision.due:
                    echo(f"{colorado_time(now)} cycle: {decision.reason}")
                    report = run_cycle(
                        workspace, log, trigger="sprint" if sprint else "schedule", window=decision.window,
                        results=decision.results, crawl_fn=crawl_fn, results_fn=results_fn, started=now,
                    )  # fmt: skip
                    echo(f"  {report.crawl} results changed: {report.results_changed}; errors: {report.errors or 'none'}; {report.seconds}s")
                    continue
                if decision.reason != last_reason:
                    echo(f"{colorado_time(now)} idle: {decision.reason}")
                    last_reason = decision.reason
            sleep(max(1.0, decision.wait_seconds))
    except BaseException as exc:
        ending = f"{type(exc).__name__}: {exc}"[:500] if str(exc) else type(exc).__name__
        raise
    finally:
        try:
            with EventLog.open(workspace_dir) as log:
                log.append("schedule.stopped", ACTOR, data={"reason": ending})
        except Exception as exc:  # noqa: BLE001 - recording the end must not hide why it ended
            echo(f"could not record the scheduler's end: {exc}")


def _state(schedule: Schedule | None, sprint: Sprint | None, decision: Decision) -> str:
    """What the loop is doing, in words that change only when it does (not with each wait)."""
    if sprint is not None:
        return f"sprint until {colorado_time(sprint.until, seconds=False)}, every {sprint.every_seconds} s"
    if schedule is None:
        return "no schedule file"
    if decision.window is not None:
        return f"window {decision.window.name!r}"
    return decision.reason


def sprint(
    workspace_dir: Path,
    *,
    minutes: int,
    every_seconds: int,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    crawl_fn: CrawlFunction = default_crawl,
    echo: Callable[[str], None] = print,
) -> int:
    """Run cycles in the foreground every ``every_seconds`` for ``minutes``; return how many ran."""
    workspace = Workspace.open(workspace_dir)
    end = clock() + timedelta(minutes=minutes)
    every = max(MIN_SPRINT_SECONDS, every_seconds)
    ran = 0
    while clock() < end:
        began = clock()
        with EventLog.open(workspace_dir) as log:
            report = run_cycle(workspace, log, trigger="sprint (foreground)", results=False, crawl_fn=crawl_fn, started=format_server_time(began))
        if report.ran:
            ran += 1
        echo(f"{format_server_time(began)} {report.crawl} errors: {report.errors or 'none'}")
        left = every - (clock() - began).total_seconds()
        if clock() + timedelta(seconds=max(0.0, left)) >= end:
            break
        sleep(max(0.0, left))
    return ran
