"""Commands for running an election live: its setup, results, estimates and scheduled crawls.

Registered on the main command line (`corla_observer.cli`) by `register`:

- ``election init|show``: the workspace's election setup (`corla_observer.election`);
- ``results gather|show|add|check``: election night results (`corla_observer.enr`), a file downloaded by hand, and the totals check (`corla_observer.totals`);
- ``estimate show|margin|record``: the sample-count estimates (`corla_observer.estimate`);
- ``schedule init|show|run|cycle|sprint|stop``: scheduled crawls (`corla_observer.schedule`);
- ``walkthrough init|next|status``: a past audit replayed step by step (`corla_observer.walkthrough`);
- ``user copy``: copy coordinator accounts from another workspace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from corla_observer.election import DEFAULT_SETUPS, ElectionSetup, SetupError, init_setup, load_setup, setup_path
from corla_observer.localtime import colorado_time
from corla_observer.workspace import Workspace

WorkspaceOption = Annotated[Path, typer.Option("--workspace", help="Workspace directory.", show_default=False)]
ActorOption = Annotated[str, typer.Option("--actor", help="Who is responsible, as coordinator:ID.", show_default=False)]

election_app = typer.Typer(help="The election's setup: likely audit center pages, results source, calendar, estimates.", no_args_is_help=True)


def _setup_or_exit(workspace: Path) -> ElectionSetup:
    try:
        setup = load_setup(Workspace.open(workspace))
    except SetupError as exc:
        for error in exc.errors:
            typer.echo(f"error: {error}", err=True)
        raise typer.Exit(2) from None
    if setup is None:
        typer.echo(f"error: no election setup; run `corla-observer election init --workspace {workspace}`", err=True)
        raise typer.Exit(2)
    return setup


@election_app.command("init")
def election_init(
    workspace: WorkspaceOption,
    election: Annotated[str, typer.Option("--election", help=f"One of: {', '.join(DEFAULT_SETUPS)}.")] = "2026/general",
    force: Annotated[bool, typer.Option("--force", help="Replace an existing election.toml.")] = False,
    mirror: Annotated[
        Path | None,
        typer.Option("--mirror", help="For an election replayed from the public mirror (2020/general): the mirror's local checkout.", show_default=False),
    ] = None,
) -> None:
    """Write the default election setup (election.toml) to the workspace, for a coordinator to check and edit."""
    from corla_observer.rehearse import set_mirror_checkout

    store = Workspace.open(workspace)
    try:
        path = init_setup(store, election, force=force)
        setup = load_setup(store)
    except SetupError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    typer.echo(f"wrote {path}")
    if setup is not None and setup.mirror is not None:
        if mirror is None:
            typer.echo("note: its audit center is replayed from the public mirror: name a checkout with --mirror, or Fetch now fails")
        else:
            set_mirror_checkout(store, mirror.resolve())
            typer.echo(f"audit center replayed from the mirror at {mirror.resolve()}")


@election_app.command("show")
def election_show(workspace: WorkspaceOption) -> None:
    """Check the workspace's election setup and print it in short."""
    setup = _setup_or_exit(workspace)
    typer.echo(f"election: {setup.election} ({setup.title})  setup {str(setup.sha256)[:12]} in {setup_path(Workspace.open(workspace))}")
    typer.echo(f"audit center: {setup.audit_center}; {len(setup.probe_urls)} probe URLs")
    typer.echo(f"pilot counties: {', '.join(setup.pilot_counties) or 'none'}")
    for milestone in setup.calendar:
        typer.echo(f"  {milestone.key:22} {colorado_time(milestone.time, seconds=False)}")
    results = setup.results
    typer.echo(
        f"results: {results.source} {results.base_url} election {results.election_id or '(to be found from ' + ', '.join(results.discover_from) + ')'}; "
        f"final after {colorado_time(results.final_after, seconds=False) if results.final_after else 'never'}"
    )
    other = sorted(c for c, (v, _) in setup.cards_per_ballot.items() if v != 1.0)
    typer.echo("cards per ballot other than 1: " + (", ".join(f"{c} {setup.cards_per_ballot[c][0]}" for c in other) or "none"))


results_app = typer.Typer(help="Election night results, for estimating the sample counts.", no_args_is_help=True)
estimate_app = typer.Typer(help="Estimates of each targeted contest's round-one sample count.", no_args_is_help=True)


@results_app.command("gather")
def results_gather(workspace: WorkspaceOption) -> None:
    """Fetch the state's results, and the targeted counties' own, when they have changed (a few requests)."""
    from corla_observer.datamap import build_map
    from corla_observer.enr import gather, results_counties
    from corla_observer.events import EventLog

    setup = _setup_or_exit(workspace)
    store = Workspace.open(workspace)
    with EventLog.open(workspace) as log:
        report = gather(store, log, setup, counties=results_counties(build_map(log, store), setup))
    typer.echo(f"election {report.election_id or 'unknown'} version {report.version or '-'}: {'changed' if report.changed else 'unchanged'}")
    for url in report.fetched:
        typer.echo(f"fetched {url}")
    for url in report.failed:
        typer.echo(f"FAILED {url}", err=True)
    for note in report.notes:
        typer.echo(f"note: {note}")


@results_app.command("show")
def results_show(workspace: WorkspaceOption) -> None:
    """List the results file used per scope: its version, when it was generated, and whether it was fetched or uploaded by hand."""
    from corla_observer.enr import load_results
    from corla_observer.events import EventLog

    with EventLog.open(workspace) as log:
        loaded = load_results(Workspace.open(workspace), log)
    if not loaded:
        typer.echo("no results yet")
    for scope, (results, fetched) in sorted(loaded.items()):
        generated = colorado_time(fetched.generated, seconds=False) if fetched.generated else "(time not in the file)"
        typer.echo(f"{scope:12} version {fetched.version or '-':8} generated {generated}, {fetched.origin} {colorado_time(fetched.time, seconds=False)}  "
                   f"{results.form or fetched.form}, {len(results.contests)} contests, {sum(results.ballots_cast.values())} ballots cast  {fetched.sha256[:12]}")  # fmt: skip


@results_app.command("add")
def results_add(
    file: Annotated[Path, typer.Argument(help="The downloaded file: detail.xml, detail.xlsx, either zipped, summary.csv or summary.zip, or summary.json.")],
    workspace: WorkspaceOption,
    actor: ActorOption,
    source_url: Annotated[str, typer.Option("--source-url", help="Where it was downloaded from.")] = "",
    note: Annotated[str, typer.Option("--note", help="Who downloaded it, when and why (a source URL or a note is required).")] = "",
    scope: Annotated[str | None, typer.Option("--scope", help="'state' or the county whose results it is (required for a summary).")] = None,
    file_time: Annotated[str | None, typer.Option("--file-time", help="When Clarity generated it, for a file that does not say (Colorado time, 2026-11-04 21:40).")] = None,
) -> None:
    """Add a Clarity results file downloaded by hand (when the automatic fetch is blocked or behind); then check the totals and update the estimates."""
    from corla_observer.enr import ResultsError, receive_results
    from corla_observer.events import EventLog
    from corla_observer.schedule import update_after_upload

    store = Workspace.open(workspace)
    with EventLog.open(workspace) as log:
        try:
            result = receive_results(store, log, file.read_bytes(), file.name, actor, scope=scope, source_url=source_url, note=note, file_time=file_time)
        except (ResultsError, OSError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from None
        if not result.accepted:
            typer.echo(f"rejected: {result.reason}", err=True)
            raise typer.Exit(1)
        generated = colorado_time(result.generated, seconds=False) if result.generated else "not known"
        typer.echo(f"recorded (event {result.seq}): {result.scope}'s {result.form}, generated {generated}, {result.contests} contests, "
                   f"{result.ballots_cast} ballots cast  {str(result.sha256)[:12]}")  # fmt: skip
        for line in result.notes:
            typer.echo(f"note: {line}")
        for line in update_after_upload(store, log):
            typer.echo(line)


@results_app.command("check")
def results_check(
    workspace: WorkspaceOption,
    record: Annotated[bool, typer.Option("--record", help="Record the check (a totals.checked event) if its inputs changed.")] = False,
) -> None:
    """The totals check: the results against the other results files, the redacted CVRs, CORLA's tallies and the manifests, one line per difference."""
    from corla_observer.datamap import build_map
    from corla_observer.events import EventLog
    from corla_observer.totals import check_totals, last_totals_check, record_totals_check, report_lines

    store = Workspace.open(workspace)
    setup = load_setup(store)
    with EventLog.open(workspace) as log:
        datamap = build_map(log, store)
        if record:
            seq = record_totals_check(store, log, datamap, setup)
            typer.echo("unchanged" if seq is None else f"recorded (event {seq})")
            found = last_totals_check(log)
            data: dict[str, Any] = found[2] if found else {"differences": [], "compared": 0}
        else:
            report = check_totals(store, log, datamap, setup)
            data = {"differences": [d.to_dict() for d in report.differences], "compared": report.compared, "notes": list(report.notes)}
    for line in report_lines(data):
        typer.echo(line)
    notes = data.get("notes")
    for note in notes if isinstance(notes, list) else []:
        typer.echo(f"note: {note}")


def current_estimates(workspace: Path) -> tuple[list[object], ElectionSetup | None]:
    """The estimates from the workspace as it is now (its data map, setup, results and margin entries)."""
    from corla_observer.datamap import build_map
    from corla_observer.enr import load_results
    from corla_observer.estimate import estimate_contests
    from corla_observer.events import EventLog

    store = Workspace.open(workspace)
    setup = load_setup(store)
    with EventLog.open(workspace) as log:
        found = estimate_contests(build_map(log, store), setup=setup, results=load_results(store, log), log=log)
    return list(found), setup


@estimate_app.command("show")
def estimate_show(
    workspace: WorkspaceOption,
    county: Annotated[list[str] | None, typer.Option("--county", help="Only contests in these counties, and statewide ones.")] = None,
) -> None:
    """Each targeted contest's estimated round-one count (central and low), with the margin's and the denominator's sources."""
    from corla_observer.estimate import ContestEstimate

    found, _ = current_estimates(workspace)
    wanted = {c.lower() for c in county or []}
    for e in found:
        assert isinstance(e, ContestEstimate)
        if wanted and not e.statewide and not {c.lower() for c in e.counties} <= wanted:
            continue
        count = f"CORLA {e.corla_count}" if e.exact else f"central {e.central} low {e.low}"
        typer.echo(f"{e.name}: {count}")
        typer.echo(f"    margin {e.margin}: {e.margin_source}")
        typer.echo(f"    ballot cards {e.denominator} (low {e.low_denominator}): {e.denominator_source}" + (f"; {e.cards_note}" if e.cards_note else ""))
        if e.exact and e.from_results is not None and e.from_results != e.corla_count:
            typer.echo(f"    the estimate from these was {e.from_results}")
        for note in e.notes:
            typer.echo(f"    note: {note}")


@estimate_app.command("record")
def estimate_record(workspace: WorkspaceOption, actor: ActorOption = "system") -> None:
    """Record the current estimates in the event log, if anything changed since the last."""
    from corla_observer.enr import load_results
    from corla_observer.estimate import ContestEstimate, record_estimates
    from corla_observer.events import EventLog

    found, setup = current_estimates(workspace)
    with EventLog.open(workspace) as log:
        seq = record_estimates(log, [e for e in found if isinstance(e, ContestEstimate)], setup=setup,
                               results=load_results(Workspace.open(workspace), log), actor=actor)  # fmt: skip
    typer.echo("unchanged" if seq is None else f"recorded (event {seq})")


schedule_app = typer.Typer(help="Scheduled crawls of the audit center and the results, and sprints.", no_args_is_help=True)


def _schedule_or_exit(workspace: Path) -> object:
    from corla_observer.schedule import ScheduleError, load_schedule

    try:
        return load_schedule(Workspace.open(workspace))
    except ScheduleError as exc:
        for error in exc.errors:
            typer.echo(f"error: {error}", err=True)
        raise typer.Exit(2) from None


@schedule_app.command("init")
def schedule_init(
    workspace: WorkspaceOption,
    election: Annotated[str, typer.Option("--election")] = "2026/general",
    force: Annotated[bool, typer.Option("--force", help="Replace an existing schedule.toml.")] = False,
) -> None:
    """Write the default crawl schedule (schedule.toml) to the workspace."""
    from corla_observer.schedule import ScheduleError, init_schedule

    try:
        typer.echo(f"wrote {init_schedule(Workspace.open(workspace), election, force=force)}")
    except ScheduleError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None


@schedule_app.command("show")
def schedule_show(workspace: WorkspaceOption) -> None:
    """The schedule's windows, any sprint, the last cycles, and what the scheduler would do now."""
    from datetime import UTC, datetime

    from corla_observer.events import EventLog, format_server_time
    from corla_observer.schedule import Schedule, active_sprint, decide, last_cycle_start

    schedule = _schedule_or_exit(workspace)
    now = format_server_time(datetime.now(UTC))
    if isinstance(schedule, Schedule):
        typer.echo(f"keep_awake: {schedule.keep_awake}")
        for w in schedule.windows:
            typer.echo(f"  {w.name:20} {colorado_time(w.start, seconds=False)} to {colorado_time(w.end, seconds=False)} every {w.every_seconds}s{' with results' if w.results else ''}")
    else:
        typer.echo("no schedule.toml")
    with EventLog.open(workspace) as log:
        sprint = active_sprint(log, now)
        last = last_cycle_start(log)
        cycles = [e for e in log.iter_events() if e.kind == "schedule.cycle"][-5:]
    if sprint:
        typer.echo(f"sprint until {colorado_time(sprint.until)} every {sprint.every_seconds}s (requested by {sprint.actor})")
    decision = decide(schedule if isinstance(schedule, Schedule) else None, sprint, last, now)
    typer.echo(f"now: {'a cycle is due' if decision.due else 'idle'} ({decision.reason})")
    for e in cycles:
        typer.echo(f"  cycle {colorado_time(e.server_time)} {e.data.get('trigger')} {e.data.get('window') or ''} {e.data.get('crawl')} {e.data.get('errors') or ''}")


@schedule_app.command("run")
def schedule_run(workspace: WorkspaceOption) -> None:
    """Run the scheduler until stopped: a cycle whenever the schedule or a sprint says (for a long-running service)."""
    from corla_observer.schedule import run_loop

    run_loop(workspace, echo=lambda line: typer.echo(line))


@schedule_app.command("cycle")
def schedule_cycle(
    workspace: WorkspaceOption,
    results: Annotated[bool, typer.Option("--results/--no-results", help="Gather election night results too.")] = True,
) -> None:
    """Run one cycle now: crawl, results, flags, estimates, prediction and interim extracts."""
    from corla_observer.events import EventLog
    from corla_observer.schedule import run_cycle

    with EventLog.open(workspace) as log:
        report = run_cycle(Workspace.open(workspace), log, trigger="command", results=results)
    if not report.ran:
        typer.echo("another cycle is running; skipped")
        raise typer.Exit(1)
    typer.echo(f"crawl {report.crawl}; results changed: {report.results_changed}; flags recorded {report.flags_recorded}; "
               f"estimate event {report.estimate_seq}; prediction event {report.prediction_seq}; extracts {report.extracts or 'unchanged'}; {report.seconds}s")  # fmt: skip
    for error in report.errors:
        typer.echo(f"error: {error}", err=True)


@schedule_app.command("sprint")
def schedule_sprint(
    workspace: WorkspaceOption,
    actor: ActorOption,
    minutes: Annotated[int, typer.Option("--minutes", min=1, max=1440, help="How long.")] = 180,
    every: Annotated[int, typer.Option("--every", min=30, help="Seconds between cycles (at least 30).")] = 60,
    here: Annotated[bool, typer.Option("--here", help="Run the cycles in this terminal instead of asking the scheduler.")] = False,
) -> None:
    """Crawl continuously (roll morning): ask the running scheduler to, or with --here run the cycles here."""
    from corla_observer.events import EventLog
    from corla_observer.schedule import request_sprint, sprint

    if here:
        typer.echo(f"{sprint(workspace, minutes=minutes, every_seconds=every, echo=lambda line: typer.echo(line))} cycles run")
        return
    with EventLog.open(workspace) as log:
        seq = request_sprint(log, actor, minutes=minutes, every_seconds=every)
    typer.echo(f"sprint requested (event {seq}); the scheduler picks it up within 20 seconds")


@schedule_app.command("stop")
def schedule_stop(workspace: WorkspaceOption, actor: ActorOption) -> None:
    """End a sprint early."""
    from corla_observer.events import EventLog
    from corla_observer.schedule import stop_sprint

    with EventLog.open(workspace) as log:
        typer.echo(f"sprint stopped (event {stop_sprint(log, actor)})")


walkthrough_app = typer.Typer(help="A past audit replayed step by step, to follow on the web pages.", no_args_is_help=True)


@walkthrough_app.command("init")
def walkthrough_init(
    workspace: WorkspaceOption,
    county: Annotated[str, typer.Option("--county", help="The county followed (La Plata, Morgan or Weld).")] = "Weld",
    election: Annotated[str, typer.Option("--election")] = "2026/primary",
    mirror: Annotated[Path | None, typer.Option("--mirror", help="A checkout of the audit center mirror (default: $CORLA_OBSERVER_MIRROR).")] = None,
    accounts_from: Annotated[Path | None, typer.Option("--accounts-from", help="Copy the coordinator accounts of this workspace.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Start an existing walkthrough again from nothing (its accounts are kept).")] = False,
) -> None:
    """Make a walkthrough workspace; then serve it (`corla-observer serve --workspace ...`) and step through it at /coord/walkthrough."""
    import os

    from corla_observer.walkthrough import WalkthroughError, init_walkthrough

    repo = mirror or (Path(os.environ["CORLA_OBSERVER_MIRROR"]) if os.environ.get("CORLA_OBSERVER_MIRROR") else None)
    if repo is None:
        typer.echo("error: give --mirror or set CORLA_OBSERVER_MIRROR", err=True)
        raise typer.Exit(2)
    try:
        init_walkthrough(workspace, election=election, county=county, mirror=repo, accounts_from=accounts_from, force=force)
    except WalkthroughError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    typer.echo(f"walkthrough of {election} for {county} ready in {workspace}")


@walkthrough_app.command("status")
def walkthrough_status(workspace: WorkspaceOption) -> None:
    """The walkthrough's steps, and which have been applied."""
    from corla_observer.events import EventLog
    from corla_observer.walkthrough import state, steps_table

    with EventLog.open(workspace) as log:
        walk = state(Workspace.open(workspace), log)
    for n, step, done in steps_table(walk):
        typer.echo(f"{'done' if done else '    '} {n:2}. {colorado_time(step.utc, seconds=False)}  {step.title if done else step.preview}")
        if done and step.overrides_note:
            typer.echo(f"         overrides applied: {step.overrides_note}")


@walkthrough_app.command("next")
def walkthrough_next(
    workspace: WorkspaceOption,
    steps: Annotated[int, typer.Option("--steps", min=1, help="How many steps to apply.")] = 1,
) -> None:
    """Apply the next step (or several) and run the pipeline."""
    from corla_observer.events import EventLog
    from corla_observer.walkthrough import WalkthroughError, next_step

    store = Workspace.open(workspace)
    with EventLog.open(workspace) as log:
        for _ in range(steps):
            try:
                step, notes = next_step(store, log)
            except WalkthroughError as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(1) from None
            typer.echo(f"{colorado_time(step.utc, seconds=False)}  {step.title}")
            for note in notes:
                typer.echo(f"    {note}")


def user_copy(
    workspace: WorkspaceOption,
    source: Annotated[Path, typer.Option("--from", help="The workspace whose coordinator accounts are copied.")],
) -> None:
    """Copy another workspace's coordinator accounts (not its observer or reader links) into this one."""
    from corla_observer.walkthrough import copy_coordinator_accounts

    Workspace.open(workspace)
    if not copy_coordinator_accounts(source, workspace):
        typer.echo(f"error: no coordinator accounts in {source}", err=True)
        raise typer.Exit(2)
    typer.echo(f"copied the coordinator accounts of {source}")


def register(app: typer.Typer) -> None:
    """Add this module's command groups to ``app``."""
    app.add_typer(election_app, name="election")
    app.add_typer(results_app, name="results")
    app.add_typer(estimate_app, name="estimate")
    app.add_typer(schedule_app, name="schedule")
    app.add_typer(walkthrough_app, name="walkthrough")
