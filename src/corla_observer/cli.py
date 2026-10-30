"""Command-line entry point."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from typer.core import TyperGroup

from corla_observer.classify import canonical_county, classify_file, extract_seed, known_county
from corla_observer.datamap import (
    AUDIT_TIMES,
    OVERRIDES_FILENAME,
    DataMap,
    OverridesError,
    Problem,
    append_override,
    apply_overrides,
    build_map,
    describe,
    explain,
    parse_cli_value,
    parse_overrides,
    parse_time,
    record_audit_time,
)
if TYPE_CHECKING:
    from corla_observer.due import ProblemView
from corla_observer.events import (
    EventLog,
    EventLogError,
    VerificationReport,
    verify_jsonl,
)
from corla_observer.fetch import (
    DEFAULT_ALLOWED_HOSTS,
    DEFAULT_PATH_PREFIX,
    DEFAULT_START_URL,
    CrawlReport,
    FetchPolicy,
    Fetcher,
    crawl,
)
from corla_observer.flags import (
    SEVERITIES,
    Addressed,
    Flag,
    addressed_flags,
    addressing,
    check_flags,
    filter_flags,
    flag_lines,
    record_flags,
    split_addressed,
    unflagged_problems,
)
from corla_observer.localtime import colorado_time
from corla_observer.predict import ContestComparison, Prediction, PredictionError
from corla_observer.redaction import DEFAULT_THRESHOLD, check_redaction, format_report
from corla_observer.rehearse import ACTOR as REHEARSAL_ACTOR
from corla_observer.rehearse import (
    ELECTION,
    MIRROR_REPOSITORY,
    PRIMARY_2026_COMMIT_IDS,
    PRIMARY_2026_COMMITS,
    PRIMARY_2026_COUNTY_FILES,
    PRIMARY_2026_ROLL_SOURCE,
    PRIMARY_2026_ROLL_TIME,
    PRIMARY_2026_SEED,
    PRIMARY_2026_SEED_SOURCE,
    PRIMARY_2026_UPLOADS_COMMIT,
    RehearsalError,
    run_all,
    stage_compare,
    stage_fetch,
    stage_overrides,
    stage_predict,
    stage_seed,
    stage_uploads,
)
from corla_observer.intake import KINDS as INTAKE_KINDS
from corla_observer.intake import known_counties, take
from corla_observer.uploads import KINDS as UPLOAD_KINDS
from corla_observer.uploads import REQUIRED_KINDS, UploadError, record_audit_boards
from corla_observer.uploads import status as upload_status
from corla_observer.workspace import Workspace

app = typer.Typer(
    help="Support for independent public observation of Colorado's risk-limiting audits.",
    no_args_is_help=True,
    add_completion=False,
)
log_app = typer.Typer(
    help=(
        "Inspect, verify and export event logs. PATH is a log directory (holding "
        "events.sqlite3 and events.jsonl) or an events.sqlite3 file."
    ),
    no_args_is_help=True,
)
app.add_typer(log_app, name="log")

LogPath = Annotated[Path, typer.Argument(help="Log directory or database file.", show_default=False)]


def _version_callback(value: bool) -> None:
    if value:
        from corla_observer.version import software

        typer.echo(software())
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    """Support for independent public observation of Colorado's risk-limiting audits."""


def _open_read_only(path: Path) -> EventLog:
    try:
        return EventLog.open(path, read_only=True)
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None


def _print_report(report: VerificationReport) -> None:
    if report.ok:
        typer.echo(f"OK: {report.event_count} events, head {report.head_seq} {report.head_hash}")
        return
    typer.echo(
        f"FAILED: {report.event_count} events verified before the first problem "
        f"(last good: {report.head_seq} {report.head_hash})",
        err=True,
    )
    for error in report.errors:
        typer.echo(f"  {error}", err=True)


@log_app.command("verify")
def log_verify(
    path: Annotated[
        Path,
        typer.Argument(
            help=(
                "Log directory, database file, or a JSONL file on its own (checked without "
                "any database)."
            ),
            show_default=False,
        ),
    ],
) -> None:
    """Recompute every hash and link; with a database, also cross-check it against its JSONL.

    Exits 0 if the log is intact and 1 at the first problem.
    """
    if path.suffix == ".jsonl" and path.is_file():
        report = verify_jsonl(path)
    else:
        with _open_read_only(path) as log:
            report = log.verify()
    _print_report(report)
    if not report.ok:
        raise typer.Exit(1)


@log_app.command("export")
def log_export(
    path: LogPath,
    dest: Annotated[Path, typer.Argument(help="JSONL file to write.", show_default=False)],
    force: Annotated[bool, typer.Option("--force", help="Overwrite DEST if it exists.")] = False,
) -> None:
    """Write the log's events, as recorded in its database, to DEST as JSONL."""
    if dest.exists() and not force:
        typer.echo(f"error: {dest} exists (use --force to overwrite)", err=True)
        raise typer.Exit(2)
    with _open_read_only(path) as log:
        count = log.export_jsonl(dest)
        seq, head_hash = log.head()
    typer.echo(f"exported {count} events, head {seq} {head_hash}")


@log_app.command("head")
def log_head(path: LogPath) -> None:
    """Print the last event's seq and hash (0 and 64 zeros for an empty log)."""
    try:
        with _open_read_only(path) as log:
            seq, head_hash = log.head()
    except EventLogError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"{seq} {head_hash}")


@app.command("fetch")
def fetch_command(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", help="Workspace directory for the fetched files.", show_default=False),
    ],
    url: Annotated[str, typer.Argument(help="The Audit Center page to start from.")] = DEFAULT_START_URL,
    log_path: Annotated[
        Path | None,
        typer.Option(
            "--log",
            help="Event log directory or events.sqlite3 file (default: the workspace directory).",
            show_default=False,
        ),
    ] = None,
    conditional: Annotated[
        bool,
        typer.Option(
            "--conditional/--refetch-all",
            help=(
                "Skip downloading files unchanged since the last fetch, by conditional GET, "
                "where the server gave an ETag or Last-Modified. The default re-fetches everything."
            ),
        ),
    ] = False,
    allow_host: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-host",
            help=f"Host to fetch from (repeatable; default {', '.join(sorted(DEFAULT_ALLOWED_HOSTS))}).",
            show_default=False,
        ),
    ] = None,
    path_prefix: Annotated[
        str, typer.Option("--path-prefix", help="Follow links only to paths under this prefix.")
    ] = DEFAULT_PATH_PREFIX,
    max_depth: Annotated[int, typer.Option("--max-depth", help="Links to follow from the start page.")] = 2,
    max_urls: Annotated[int, typer.Option("--max-urls", help="Most URLs to fetch.")] = 2000,
    rate: Annotated[
        float,
        typer.Option(
            "--rate",
            min=0.01,
            help="Requests per second at most; halved automatically on a 429, a 503 or a Cloudflare challenge.",
        ),
    ] = 1 / FetchPolicy().min_interval,
) -> None:
    """Fetch the Audit Center page and the files it links to, recording every fetch.

    Stores each file in the workspace, records fetches, changes, missing
    files, classifications and alerts in the event log, and prints a
    summary. Exits 1 if the start page could not be fetched.
    """
    policy = FetchPolicy(
        allowed_hosts=frozenset(allow_host) if allow_host else DEFAULT_ALLOWED_HOSTS,
        min_interval=1 / rate,
    )
    store = Workspace.open(workspace)
    fetcher = Fetcher(policy)
    with EventLog.open(log_path if log_path is not None else workspace) as log:
        report = crawl(
            url,
            workspace=store,
            log=log,
            fetcher=fetcher,
            conditional=conditional,
            path_prefix=path_prefix,
            max_depth=max_depth,
            max_urls=max_urls,
        )
    for entry in report.entries:
        detail = entry.reason or " ".join(part for part in (entry.kind, entry.county) if part)
        typer.echo(f"{entry.outcome:12} {entry.url}" + (f"  [{detail}]" if detail else ""))
    counts = ", ".join(
        f"{report.count(o)} {o}" for o in ("new", "changed", "unchanged", "not_modified", "missing")
    )
    typer.echo(f"{len(report.entries)} URLs: {counts}; {len(report.skipped_links)} links not followed")
    for alert in report.alerts:
        typer.echo(f"ALERT: {json.dumps(alert, sort_keys=True)}", err=True)
    for slowdown in fetcher.slowdowns:
        typer.echo(f"slowed down: {json.dumps(slowdown, sort_keys=True)}", err=True)
    if report.start_failed:
        typer.echo(f"error: could not fetch {url}", err=True)
        raise typer.Exit(1)


@app.command("classify")
def classify_command(
    files: Annotated[list[Path], typer.Argument(help="Files to classify.", show_default=False)],
) -> None:
    """Print each file's kind, county and the evidence for them, judged by content alone."""
    for path in files:
        result = classify_file(path)
        typer.echo(
            f"{path}\t{result.kind}\t{result.county or '-'}\t"
            f"{json.dumps(result.evidence, ensure_ascii=False, sort_keys=True)}"
        )


class _SeedGroup(TyperGroup):
    """The ``seed`` commands; ``seed FILE``, the original form, is ``seed read FILE``."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["read", *args]
        return super().parse_args(ctx, args)


seed_app = typer.Typer(
    cls=_SeedGroup,
    help=(
        "The random seed: read it from a State Audit Report, or enter it by hand (`seed set`), in effect at once; "
        "another coordinator may enter it again to check it (`seed confirm`)."
    ),
    no_args_is_help=True,
)
app.add_typer(seed_app, name="seed")


@seed_app.command("read")
def seed_command(
    file: Annotated[Path, typer.Argument(help="A State Audit Report .xlsx file.", show_default=False)],
) -> None:
    """Print the "Audit Random Seed" from a State Audit Report workbook's Summary sheet.

    Exits 1 if the workbook has no readable seed.
    """
    seed = extract_seed(file.read_bytes())
    if seed is None:
        typer.echo(f"error: no Audit Random Seed found in {file}", err=True)
        raise typer.Exit(1)
    typer.echo(seed)


WorkspaceOption = Annotated[
    Path, typer.Option("--workspace", help="Workspace directory.", show_default=False)
]
LogOption = Annotated[
    Path | None,
    typer.Option(
        "--log",
        help="Event log directory or events.sqlite3 file (default: the workspace directory).",
        show_default=False,
    ),
]
CountyOption = Annotated[str, typer.Option("--county", help="The county, e.g. 'La Plata'.", show_default=False)]
ActorOption = Annotated[
    str, typer.Option("--actor", help="Who is responsible, as coordinator:ID.", show_default=False)
]
ThresholdOption = Annotated[
    int, typer.Option("--threshold", min=1, help="Smallest group of identical ballots allowed.")
]


MIRROR_ENV = "CORLA_OBSERVER_MIRROR"


@app.command("upload")
def upload_command(
    workspace: WorkspaceOption,
    actor: ActorOption,
    files: Annotated[list[Path], typer.Argument(help="The files the county provided (or results files downloaded by hand).", show_default=False)],
    county: Annotated[
        str | None, typer.Option("--county", help="Override the detected county ('state' for a results file).", show_default=False)
    ] = None,
    kind: Annotated[
        str | None, typer.Option("--kind", help=f"Override the detected kind: one of {', '.join(INTAKE_KINDS)}.", show_default=False)
    ] = None,
    log_path: LogOption = None,
    threshold: ThresholdOption = DEFAULT_THRESHOLD,
    source_url: Annotated[str, typer.Option("--source-url", help="For a results file: where it was downloaded from.")] = "",
    note: Annotated[str, typer.Option("--note", help="For a results file: who downloaded it, when and why.")] = "",
) -> None:
    """Receive county files: detect each one's kind and county from its content, check it, store it and record it.

    The kind (redacted CVR, manifest, pull list, results) and the county are
    detected from each file's content (see corla_observer.intake), with the
    evidence printed; --kind and --county override them. A redacted CVR is
    checked first (see check-redaction) and is stored only if it passes;
    otherwise its bytes are discarded and only its hash and the counts-only
    report are recorded. Exits 0 if every file was accepted, 1 if any was
    rejected, 2 for a bad request or a file whose kind or county could not
    be told (give --kind or --county).
    """
    for file in files:
        if not file.is_file():
            typer.echo(f"error: no such file: {file}", err=True)
            raise typer.Exit(2)
    store = Workspace.open(workspace)
    codes: list[int] = []
    results_arrived = False
    with EventLog.open(log_path if log_path is not None else workspace) as log:
        known = known_counties(store, log)
        for file in files:
            try:
                intake = take(store, log, file, file.name, actor, kind=kind, county=county, known=known, threshold=threshold,
                              source_url=source_url, note=note)  # fmt: skip
            except UploadError as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(2) from None
            d = intake.detection
            whose = "the state" if d.county == "state" else (d.county or "county not known")
            typer.echo(f"{file.name}: detected {d.kind or 'kind not known'} for {whose}" + (f" (chosen: {', '.join(d.chosen)})" if d.chosen else ""))
            for line in d.evidence:
                typer.echo(f"  evidence: {line}")
            for warning in d.warnings:
                typer.echo(f"warning: {warning}", err=True)
            if not d.complete:
                candidates = d.counties[:12]
                typer.echo(
                    f"error: {file.name}: {intake.error}; give "
                    + " and ".join(x for x, need in (("--kind", d.kind is None), ("--county", d.county is None)) if need)
                    + (f" (candidates: {', '.join(candidates)})" if d.county is None and candidates else ""),
                    err=True,
                )
                codes.append(2)
                continue
            if intake.results is not None or intake.error:
                res = intake.results
                if res is not None and res.accepted:
                    results_arrived = True
                    typer.echo(f"{whose} results {intake.original_name}: accepted; sha256 {res.sha256}, {res.form}, {res.contests} contests")
                else:
                    typer.echo(f"{whose} results {intake.original_name}: REJECTED ({res.reason if res else intake.error})")
                codes.append(0 if res is not None and res.accepted else 1)
                continue
            result = intake.upload
            assert result is not None
            outcome = "accepted" if result.accepted else f"REJECTED ({result.reason})"
            typer.echo(
                f"{result.county} {result.kind} {result.original_name}: {outcome}; "
                f"sha256 {result.sha256 or '-'}, {result.size} bytes"
            )
            if result.report is not None:
                typer.echo(format_report(result.report))
            for warning in result.warnings:
                typer.echo(f"warning: {warning}", err=True)
            codes.append(0 if result.accepted else 1)
        if results_arrived:
            from corla_observer.schedule import update_after_upload

            for line in update_after_upload(store, log):
                typer.echo(line)
    if 2 in codes:
        raise typer.Exit(2)
    if 1 in codes:
        raise typer.Exit(1)


@app.command("audit-boards")
def audit_boards_command(
    workspace: WorkspaceOption,
    county: CountyOption,
    count: Annotated[int, typer.Option("--count", help="Number of audit boards.", show_default=False)],
    actor: ActorOption,
    log_path: LogOption = None,
) -> None:
    """Record how many audit boards a county will use."""
    with EventLog.open(log_path if log_path is not None else workspace) as log:
        try:
            seq = record_audit_boards(log, county, count, actor)
        except UploadError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from None
    typer.echo(f"recorded {count} audit board(s) for {known_county(county) or county} (event {seq})")


@app.command("status")
def status_command(
    workspace: WorkspaceOption,
    log_path: LogOption = None,
    county: Annotated[
        list[str] | None,
        typer.Option(
            "--county", help="Also list this county if nothing has arrived (repeatable).", show_default=False
        ),
    ] = None,
) -> None:
    """Show, per county, the latest file of each kind, the audit boards, and what is missing."""
    path = log_path if log_path is not None else workspace
    with _open_read_only(path) as log:
        try:
            counties = upload_status(log, county or ())
        except UploadError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from None
    if not counties:
        typer.echo("nothing received yet")
    for entry in counties.values():
        typer.echo(entry.county)
        for kind in UPLOAD_KINDS:
            received = entry.latest.get(kind)
            if received is None:
                note = "missing" if kind in REQUIRED_KINDS else "not received (optional)"
                typer.echo(f"  {kind}: {note}")
                continue
            verdict = f", {received.verdict}" if received.verdict else ""
            typer.echo(
                f"  {kind}: {received.original_name}, sha256 {received.sha256[:16]}, "
                f"{received.size} bytes{verdict}, {colorado_time(received.server_time)} by {received.actor}"
            )
            for warning in received.warnings:
                typer.echo(f"    warning: {warning}")
        boards = "missing" if entry.audit_boards is None else str(entry.audit_boards)
        if entry.audit_boards_placeholder:
            boards += " (PLACEHOLDER: not yet known)"
        typer.echo(f"  audit boards: {boards}")
        if entry.rejected:
            last = entry.last_rejection or {}
            typer.echo(
                f"  rejected uploads: {entry.rejected} (latest: {last.get('kind')} "
                f"{last.get('original_name')}, {last.get('reason')})"
            )
        missing = entry.missing
        typer.echo(f"  missing: {', '.join(missing) if missing else 'nothing'}")


@app.command("check-redaction")
def check_redaction_command(
    file: Annotated[Path, typer.Argument(help="A CVR file.", show_default=False)],
    threshold: ThresholdOption = DEFAULT_THRESHOLD,
    names: Annotated[
        bool, typer.Option("--names", help="List the contest names of each rare group's style.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
) -> None:
    """Check that a redacted CVR, as provided, leaves no group of fewer than THRESHOLD identical ballots.

    Prints counts, styles and precincts only, never votes. Exits 0 on PASS
    and 1 on FAIL.
    """
    if not file.is_file():
        typer.echo(f"error: no such file: {file}", err=True)
        raise typer.Exit(2)
    report = check_redaction(file, threshold, include_contest_names=names)
    if as_json:
        typer.echo(json.dumps(report.to_dict(max_groups=None), ensure_ascii=False, indent=1))
    else:
        typer.echo(format_report(report, max_groups=None))
    if not report.passed:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# The data map

map_app = typer.Typer(
    help=(
        "Show, explain and adjust the election's data map: which file is which, and everything a "
        "sampling run needs. Adjustments go in the workspace's overrides.toml and take effect when applied."
    ),
    no_args_is_help=True,
)
app.add_typer(map_app, name="map")

ForAiOption = Annotated[
    bool,
    typer.Option(
        "--for-ai",
        help="Print a compact, self-contained description, with the problems and the overrides format, to paste into an AI assistant.",
    ),
]
PreviewOption = Annotated[
    bool, typer.Option("--preview", help="Use the overrides file as it is now, even if not yet applied.")
]
AsOfOption = Annotated[
    str | None,
    typer.Option(
        "--as-of",
        help=(
            "Judge what is due as of this time (Colorado time unless it has an offset, such as "
            '"2026-11-13 15:00"); default: now, or a walkthrough\'s simulated time.'
        ),
        show_default=False,
    ),
]


def _view(workspace: Path, log_path: Path | None, datamap: DataMap, problems: Sequence[Problem], as_of: str | None) -> ProblemView:
    from corla_observer.due import view_for

    try:
        now = parse_time(as_of) if as_of else None
    except ValueError as exc:
        typer.echo(f"error: --as-of: {exc}", err=True)
        raise typer.Exit(2) from None
    with _open_read_only(log_path if log_path is not None else workspace) as log:
        return view_for(datamap, Workspace.open(workspace), log, problems, now=now)


def _load_map(workspace: Path, log_path: Path | None, preview: bool) -> DataMap:
    store = Workspace.open(workspace)
    overrides = None
    if preview:
        path = workspace / OVERRIDES_FILENAME
        if not path.is_file():
            typer.echo(f"error: no {OVERRIDES_FILENAME} in {workspace}", err=True)
            raise typer.Exit(2)
        try:
            overrides = parse_overrides(path.read_text(encoding="utf-8"))
        except OverridesError as exc:
            for error in exc.errors:
                typer.echo(f"error: {error}", err=True)
            raise typer.Exit(2) from None
    with _open_read_only(log_path if log_path is not None else workspace) as log:
        return build_map(log, store, preview=overrides)


def _flags(workspace: Path, log_path: Path | None, datamap: DataMap) -> list[Flag]:
    with _open_read_only(log_path if log_path is not None else workspace) as log:
        return check_flags(log, Workspace.open(workspace), datamap)


def _echo_flags(flags: Sequence[Flag], addressed: Mapping[str, Addressed] | None = None) -> None:
    high = sum(1 for f in flags if f.severity == "high")
    typer.echo(f"flags ({len(flags)}, {high} high):" if flags else "flags: none")
    for flag in flags:
        for line in flag_lines(flag):
            typer.echo(line)
        marking, holds = addressing(flag, addressed or {})
        if marking is not None:
            state = "ADDRESSED" if holds else "addressed before, with new findings since,"
            typer.echo(f"    {state} by {marking.actor} at {colorado_time(marking.time)} (event {marking.seq}): {marking.note}")


@map_app.command("show")
def map_show(
    workspace: WorkspaceOption,
    log_path: LogOption = None,
    for_ai: ForAiOption = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the whole map as JSON.")] = False,
    preview: PreviewOption = False,
    show_all: Annotated[bool, typer.Option("--all", help="List every file, not only the noteworthy ones.")] = False,
    as_of: AsOfOption = None,
) -> None:
    """Show the data map: the manifests, seed, county order and contests chosen, the files, and the problems.

    Inputs not yet due (by the election's calendar) are summed up as not yet
    expected rather than listed as problems.
    """
    datamap = _load_map(workspace, log_path, preview)
    flags = _flags(workspace, log_path, datamap)
    view = _view(workspace, log_path, datamap, unflagged_problems(datamap.check(), flags), as_of)
    if as_json:
        typer.echo(
            json.dumps(
                datamap.to_dict() | {"flags": [f.to_dict() for f in flags], "problem_view": view.to_dict()},
                ensure_ascii=False,
                indent=1,
            )
        )
    else:
        typer.echo(describe(datamap, for_ai=for_ai, show_all=show_all, flags=flags, view=view), nl=False)


@map_app.command("explain")
def map_explain(
    file: Annotated[
        str,
        typer.Argument(
            help="A URL, URL ending, file name, ref or SHA-256 prefix; or a local file, classified on its own.",
            show_default=False,
        ),
    ],
    workspace: WorkspaceOption,
    log_path: LogOption = None,
    preview: PreviewOption = False,
) -> None:
    """Explain why a file is in the map as it is: its classification evidence, overrides and use."""
    datamap = _load_map(workspace, log_path, preview)
    text = explain(datamap, file)
    if text.endswith("matches no file in the data map\n") and Path(file).is_file():
        result = classify_file(file)
        typer.echo(f"{file} is not in the data map; by content it is {result.kind}" + (f", county {result.county}" if result.county else ""))
        typer.echo(json.dumps(result.evidence, ensure_ascii=False, sort_keys=True, indent=1))
        return
    typer.echo(text, nl=False)


@map_app.command("check")
def map_check(
    workspace: WorkspaceOption,
    log_path: LogOption = None,
    county: Annotated[
        list[str] | None,
        typer.Option("--county", help="Only what these counties' contests and the statewide ones need (repeatable).", show_default=False),
    ] = None,
    for_ai: ForAiOption = False,
    preview: PreviewOption = False,
    as_of: AsOfOption = None,
) -> None:
    """List everything wrong for a sampling run, what is not yet expected, and every change flag to investigate.

    An input not yet due by the election's calendar is listed as not yet
    expected, not as a problem. Exits 1 if anything due or wrong blocks a
    sampling run (flags alone do not).
    """
    from corla_observer.due import view_lines

    datamap = _load_map(workspace, log_path, preview)
    problems = datamap.check(county or ())
    flags = filter_flags(_flags(workspace, log_path, datamap), counties=county or ())
    view = _view(workspace, log_path, datamap, unflagged_problems(problems, flags), as_of)
    if for_ai:
        typer.echo(describe(datamap, for_ai=True, flags=flags, view=view), nl=False)
    else:
        lines = view_lines(view)
        typer.echo("\n".join(lines))
        _echo_flags(flags)
    held = {p.code for p in view.held}
    if view.errors or any(p.severity == "error" and p.code not in held for p in problems):
        raise typer.Exit(1)


@map_app.command("override")
def map_override(
    section: Annotated[
        str,
        typer.Argument(
            help="file, supply, adapter, seed, county_order, county_alias, manifest_adjustment or contest.",
            show_default=False,
        ),
    ],
    workspace: WorkspaceOption,
    reason: Annotated[str, typer.Option("--reason", help="Why, and the evidence. Required.", show_default=False)],
    values: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help='KEY=VALUE (repeatable). VALUE is TOML (42, true, ["a", "b"], { k = 1 }) or plain text.',
            show_default=False,
        ),
    ] = None,
    apply: Annotated[bool, typer.Option("--apply", help="Apply the overrides file afterwards (needs --actor).")] = False,
    actor: Annotated[str | None, typer.Option("--actor", help="Who applies it, as coordinator:ID.", show_default=False)] = None,
    log_path: LogOption = None,
) -> None:
    """Add one item to the overrides file, checking the whole file first.

    For example: map override file --set match=files/LaPlata.csv --set ignore=true --reason "..."
    """
    items: dict[str, object] = {}
    for value in values or []:
        key, sep, text = value.partition("=")
        if not sep or not key.strip():
            typer.echo(f"error: --set needs KEY=VALUE, got {value!r}", err=True)
            raise typer.Exit(2)
        items[key.strip()] = parse_cli_value(text, key.strip())
    items["reason"] = reason
    try:
        append_override(workspace / OVERRIDES_FILENAME, section, items)
    except OverridesError as exc:
        for error in exc.errors:
            typer.echo(f"error: {error}", err=True)
        raise typer.Exit(2) from None
    typer.echo(f"added a {section} item to {workspace / OVERRIDES_FILENAME}")
    if apply:
        if actor is None:
            typer.echo("error: --apply needs --actor", err=True)
            raise typer.Exit(2)
        _apply(workspace, log_path, actor, None)


def _apply(workspace: Path, log_path: Path | None, actor: str, file: Path | None) -> None:
    store = Workspace.open(workspace)
    path = file or workspace / OVERRIDES_FILENAME
    if not path.is_file():
        typer.echo(f"error: no overrides file at {path}", err=True)
        raise typer.Exit(2)
    with EventLog.open(log_path if log_path is not None else workspace) as log:
        try:
            result = apply_overrides(store, log, actor, path=path)
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from None
    for name in result.supplied:
        typer.echo(f"supplied {name}")
    for name in result.adapters:
        typer.echo(f"adapter produced {name}")
    if not result.applied:
        typer.echo(f"REJECTED (event {result.event_seq}); the overrides applied before stay in effect:", err=True)
        for error in result.errors:
            typer.echo(f"  {error}", err=True)
        raise typer.Exit(1)
    typer.echo(f"applied overrides sha256 {result.sha256} (event {result.event_seq})")


@map_app.command("apply")
def map_apply(
    workspace: WorkspaceOption,
    actor: ActorOption,
    log_path: LogOption = None,
    file: Annotated[
        Path | None, typer.Option("--file", help=f"The overrides file (default: the workspace's {OVERRIDES_FILENAME}).", show_default=False)
    ] = None,
) -> None:
    """Check the overrides file, carry out its supplies and adapters, and record it as applied.

    Exits 1, leaving the overrides applied before in effect, if anything in it does not check out.
    """
    _apply(workspace, log_path, actor, file)


@app.command("flags")
def flags_command(
    workspace: WorkspaceOption,
    log_path: LogOption = None,
    severity: Annotated[
        str | None,
        typer.Option("--severity", help=f"Only flags at least this severe: {' or '.join(SEVERITIES)}.", show_default=False),
    ] = None,
    county: Annotated[
        list[str] | None,
        typer.Option("--county", help="Only flags about these counties, or about none (repeatable).", show_default=False),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the flags as JSON.")] = False,
    record: Annotated[
        bool, typer.Option("--record", help="Record the flags not yet recorded as map.alert events (needs --actor).")
    ] = False,
    actor: Annotated[str | None, typer.Option("--actor", help="Who records them, as coordinator:ID.", show_default=False)] = None,
    only_open: Annotated[bool, typer.Option("--open", help="Only flags not marked addressed.")] = False,
) -> None:
    """List every change flag: files changed after the roll, late or failed uploads, manifests unlike CORLA's import.

    Each flag has a severity, a reason and what to investigate (see
    corla_observer.flags), and says whether a coordinator marked it
    addressed (on the web, /coord/flags). Exits 1 if any high-severity flag
    that is still open is listed.
    """
    if severity is not None and severity not in SEVERITIES:
        typer.echo(f"error: --severity must be one of {', '.join(SEVERITIES)}", err=True)
        raise typer.Exit(2)
    if record and actor is None:
        typer.echo("error: --record needs --actor", err=True)
        raise typer.Exit(2)
    datamap = _load_map(workspace, log_path, False)
    flags = filter_flags(_flags(workspace, log_path, datamap), severity=severity, counties=county or ())
    with _open_read_only(log_path if log_path is not None else workspace) as log:
        addressed = addressed_flags(log.iter_events())
    open_flags, _ = split_addressed(flags, addressed)
    if only_open:
        flags = open_flags
    if record and actor is not None:
        with EventLog.open(log_path if log_path is not None else workspace) as log:
            try:
                seqs = record_flags(log, flags, actor)
            except ValueError as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(2) from None
        typer.echo(f"recorded {len(seqs)} flag(s) as map.alert events", err=True)
    if as_json:
        listed = []
        for flag in flags:
            marking, holds = addressing(flag, addressed)
            listed.append(flag.to_dict() | {"addressed": marking.to_dict() if marking else None, "addressed_holds": holds})
        typer.echo(json.dumps(listed, ensure_ascii=False, indent=1))
    else:
        _echo_flags(flags, addressed)
    if any(f.severity == "high" for f in open_flags):
        raise typer.Exit(1)


@app.command("audit-time")
def audit_time_command(
    which: Annotated[str, typer.Argument(help=f"{' or '.join(AUDIT_TIMES)}.", show_default=False)],
    moment: Annotated[
        str,
        typer.Argument(help="When: '2026-11-16 10:10' (Colorado time) or ISO 8601 with an offset.", show_default=False),
    ],
    workspace: WorkspaceOption,
    actor: ActorOption,
    source: Annotated[str, typer.Option("--source", help="How it is known.", show_default=False)],
    log_path: LogOption = None,
) -> None:
    """Record when the dice roll took place (roll) or the first round started (round_start).

    The data map uses the earliest roll time known (this, the seed's entry, or a
    [roll_time] override), and flags files that change after it.
    """
    with EventLog.open(log_path if log_path is not None else workspace) as log:
        try:
            seq = record_audit_time(log, which, moment, actor, source)
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from None
    typer.echo(f"recorded {which} {colorado_time(parse_time(moment))} (event {seq})")


# ---------------------------------------------------------------------------
# Rehearsal

rehearse_app = typer.Typer(
    help=(
        "Re-run parts of the July 2026 primary audit from the data gathered then (see docs/REHEARSAL.md). "
        "Each stage can be run on its own."
    ),
    no_args_is_help=True,
)
app.add_typer(rehearse_app, name="rehearse")

MirrorOption = Annotated[
    Path | None,
    typer.Option(
        "--mirror",
        help="A git checkout of the audit center mirror (default: $CORLA_OBSERVER_MIRROR).",
        show_default=False,
    ),
]
RehearsalActorOption = Annotated[str, typer.Option("--actor", help="Who is responsible, as coordinator:ID.")]


def _mirror(mirror: Path | None) -> Path:
    path = mirror or (Path(os.environ[MIRROR_ENV]) if os.environ.get(MIRROR_ENV) else None)
    if path is None or not (path / ".git").exists():
        typer.echo(f"error: give --mirror or set {MIRROR_ENV} to a git checkout of {MIRROR_REPOSITORY}", err=True)
        raise typer.Exit(2)
    return path


def _report_fetch(commit: str, report: CrawlReport) -> None:
    counts = ", ".join(f"{report.count(o)} {o}" for o in ("new", "changed", "unchanged", "missing"))
    typer.echo(f"fetched from the mirror at {commit[:12]}: {len(report.entries)} URLs: {counts}")
    for alert in report.alerts:
        typer.echo(f"  ALERT: {json.dumps(alert, sort_keys=True)}")


def _rehearsal_error(exc: Exception) -> typer.Exit:
    typer.echo(f"error: {exc}", err=True)
    return typer.Exit(1)


@rehearse_app.command("commits")
def rehearse_commits() -> None:
    """List the mirror commits of the 2026 primary, oldest first, with what each holds."""
    for commit, description in PRIMARY_2026_COMMITS:
        typer.echo(f"{commit}  {description}")


@rehearse_app.command("fetch")
def rehearse_fetch(
    workspace: WorkspaceOption,
    commit: Annotated[str, typer.Option("--commit", help="The mirror commit (see `rehearse commits`).", show_default=False)],
    mirror: MirrorOption = None,
    election: Annotated[str, typer.Option("--election", help="The election's directory in the mirror.")] = ELECTION,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Leave out this path under the election's directory, or every path starting with it (repeatable).", show_default=False),
    ] = None,
) -> None:
    """Fetch the audit center files as the mirror held them at COMMIT, logged as a fetch marked simulated."""
    store = Workspace.open(workspace)
    with EventLog.open(workspace) as log:
        try:
            report = stage_fetch(store, log, _mirror(mirror), commit, election=election, exclude=tuple(exclude or ()))
        except RehearsalError as exc:
            raise _rehearsal_error(exc) from None
    _report_fetch(commit, report)


@rehearse_app.command("uploads")
def rehearse_uploads(
    workspace: WorkspaceOption,
    mirror: MirrorOption = None,
    commit: Annotated[str, typer.Option("--commit", help="The mirror commit to read the county files at.")] = PRIMARY_2026_UPLOADS_COMMIT,
    county: Annotated[
        list[str] | None, typer.Option("--county", help="Only these counties (repeatable).", show_default=False)
    ] = None,
    audit_boards: Annotated[
        list[str] | None,
        typer.Option("--audit-boards", help="COUNTY=N, a known audit board count (repeatable); others are placeholders.", show_default=False),
    ] = None,
    actor: RehearsalActorOption = REHEARSAL_ACTOR,
) -> None:
    """Upload the counties' redacted CVRs and manifests through the real upload path, and their audit boards."""
    boards: dict[str, int] = {}
    for value in audit_boards or []:
        name, _, number = value.partition("=")
        if not number.strip().isdigit():
            typer.echo(f"error: --audit-boards needs COUNTY=N, got {value!r}", err=True)
            raise typer.Exit(2)
        boards[canonical_county(name) or name] = int(number)
    store = Workspace.open(workspace)
    with EventLog.open(workspace) as log:
        try:
            report = stage_uploads(
                store, log, _mirror(mirror), commit=commit, counties=county or tuple(PRIMARY_2026_COUNTY_FILES),
                audit_boards=boards, actor=actor,
            )
        except (RehearsalError, UploadError) as exc:
            raise _rehearsal_error(exc) from None
    for result in report.results:
        outcome = "accepted" if result.accepted else f"REJECTED ({result.reason})"
        verdict = f", redaction {result.report.verdict}" if result.report else ""
        typer.echo(f"{result.county} {result.kind} {result.original_name}: {outcome}{verdict}")
        for warning in result.warnings:
            typer.echo(f"  warning: {warning}")
    for name, (count, placeholder) in report.audit_boards.items():
        typer.echo(f"{name} audit boards: {count}" + (" (PLACEHOLDER)" if placeholder else ""))


@rehearse_app.command("overrides")
def rehearse_overrides(workspace: WorkspaceOption, actor: RehearsalActorOption = REHEARSAL_ACTOR) -> None:
    """Write the primary's overrides file (if the workspace has none) and apply it."""
    store = Workspace.open(workspace)
    with EventLog.open(workspace) as log:
        result = stage_overrides(store, log, actor=actor)
    if not result.applied:
        for error in result.errors:
            typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1)
    typer.echo(f"applied {workspace / OVERRIDES_FILENAME} (sha256 {result.sha256[:16]}, event {result.event_seq})")


@rehearse_app.command("seed")
def rehearse_seed(
    workspace: WorkspaceOption,
    seed: Annotated[str, typer.Option("--seed", help="The seed.")] = PRIMARY_2026_SEED,
    source: Annotated[str, typer.Option("--source", help="Where it was announced or read.")] = PRIMARY_2026_SEED_SOURCE,
    roll_time: Annotated[
        str, typer.Option("--roll-time", help="When the roll took place (empty: record none).")
    ] = PRIMARY_2026_ROLL_TIME,
    roll_source: Annotated[str, typer.Option("--roll-source", help="How the roll time is known.")] = PRIMARY_2026_ROLL_SOURCE,
    actor: RehearsalActorOption = REHEARSAL_ACTOR,
) -> None:
    """Enter the seed (marked rehearsal), and record the roll time."""
    with EventLog.open(workspace) as log:
        try:
            seq = stage_seed(log, seed=seed, source=source, actor=actor, roll_time=roll_time or None, roll_source=roll_source)
        except ValueError as exc:
            raise _rehearsal_error(exc) from None
    typer.echo(f"seed {seed} entered (event {seq})")


def _print_prediction(prediction: Prediction, counties: Sequence[str]) -> None:
    typer.echo(f"seed {prediction.seed}; county order: {prediction.county_order_source}")
    for contest in prediction.contests:
        estimated = " (ESTIMATED count)" if contest.estimated else ""
        typer.echo(
            f"{contest.name}: {contest.sample_count} draws{estimated} over {contest.domain_size} ballot cards"
            + (f"; adjusted: {'; '.join(contest.adjustments)}" if contest.adjustments else "")
        )
        if contest.statewide:
            for county in counties:
                ids = [d.imprinted_id for d in contest.draws_for(county)]
                typer.echo(f"  {county}: {len(ids)} draws" + (f": {', '.join(ids[:8])}" + (" ..." if len(ids) > 8 else "") if ids else ""))
        else:
            ids = [d.imprinted_id for d in contest.draws]
            typer.echo(f"  first draws: {', '.join(ids[:8])}" + (" ..." if len(ids) > 8 else ""))


@rehearse_app.command("predict")
def rehearse_predict(
    workspace: WorkspaceOption,
    county: Annotated[
        list[str] | None, typer.Option("--county", help="Counties whose contests to predict (repeatable).", show_default=False)
    ] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Also write the prediction's JSON here.", show_default=False)] = None,
    actor: RehearsalActorOption = REHEARSAL_ACTOR,
) -> None:
    """Predict the draws for the counties' targeted contests and the statewide contest, and record them.

    Exits 1 if the data map lacks something the prediction needs; `map check` says what.
    """
    counties = county or list(PRIMARY_2026_COUNTY_FILES)
    store = Workspace.open(workspace)
    with EventLog.open(workspace) as log:
        try:
            _, prediction, seq, sha = stage_predict(store, log, counties=counties, actor=actor)
        except PredictionError as exc:
            raise _rehearsal_error(exc) from None
    _print_prediction(prediction, counties)
    typer.echo(f"recorded prediction {sha[:16]} (event {seq})")
    if out is not None:
        out.write_bytes(prediction.to_json())


@rehearse_app.command("compare")
def rehearse_compare(workspace: WorkspaceOption, actor: RehearsalActorOption = REHEARSAL_ACTOR) -> None:
    """Compare the latest prediction with CORLA's published selections. Exits 1 on any mismatch."""
    store = Workspace.open(workspace)
    with EventLog.open(workspace) as log:
        try:
            seq, results, notes = stage_compare(store, log, actor=actor)
        except RehearsalError as exc:
            raise _rehearsal_error(exc) from None
    _print_comparison(seq, results, notes)
    if any(r.status == "mismatch" for r in results):
        raise typer.Exit(1)


def _print_comparison(seq: int, results: Sequence[ContestComparison], notes: Sequence[str]) -> None:
    typer.echo(f"prediction of event {seq} against CORLA's published selections")
    for note in notes:
        typer.echo(f"  {note}")
    for r in results:
        detail = f" ({r.note})" if r.note else ""
        typer.echo(f"{r.status.upper():13} {r.name}: {r.compared} draws compared, CORLA {r.published}{detail}")
        if r.missing:
            typer.echo(f"  CORLA's, not predicted: {', '.join(r.missing[:10])}")
        if r.extra:
            typer.echo(f"  predicted, not CORLA's: {', '.join(r.extra[:10])}")


@rehearse_app.command("all")
def rehearse_all(
    workspace: WorkspaceOption,
    mirror: MirrorOption = None,
    commit: Annotated[
        list[str] | None,
        typer.Option("--commit", help="Mirror commits to fetch at, in order (default: every commit in `rehearse commits`).", show_default=False),
    ] = None,
    actor: RehearsalActorOption = REHEARSAL_ACTOR,
) -> None:
    """Run every stage on a new workspace: fetch at each commit, uploads, overrides, seed, predict, compare."""
    try:
        report = run_all(workspace, _mirror(mirror), commits=commit or PRIMARY_2026_COMMIT_IDS, actor=actor)
    except (RehearsalError, PredictionError) as exc:
        raise _rehearsal_error(exc) from None
    for fetched, crawl_report in report.fetches:
        _report_fetch(fetched, crawl_report)
    if report.uploads:
        for result in report.uploads.results:
            typer.echo(f"{result.county} {result.kind}: {'accepted' if result.accepted else 'REJECTED ' + str(result.reason)}")
    if report.overrides:
        typer.echo(f"overrides {'applied' if report.overrides.applied else 'REJECTED: ' + '; '.join(report.overrides.errors)}")
    if report.datamap is not None:
        for problem in report.datamap.check(PRIMARY_2026_COUNTY_FILES):
            typer.echo(f"map {problem.severity}: {problem.code}: {problem.message}")
    if report.prediction is not None:
        _print_prediction(report.prediction, list(PRIMARY_2026_COUNTY_FILES))
    if report.comparisons:
        _print_comparison(0, report.comparisons, report.compare_notes)
    else:
        for note in report.compare_notes:
            typer.echo(f"compare: {note}")
    high = [f for f in report.flags if f.severity == "high"]
    typer.echo(f"flags: {len(report.flags)}, {len(high)} high (`corla-observer flags --workspace {workspace}` lists them)")
    if any(r.status == "mismatch" for r in report.comparisons):
        raise typer.Exit(1)


def main() -> None:
    app()


# ---------------------------------------------------------------------------
# The web interface (corla_observer.web): coordinator accounts and the server.

user_app = typer.Typer(help="Coordinator accounts for the web interface.", no_args_is_help=True)
app.add_typer(user_app, name="user")


@user_app.command("add")
def user_add(
    name: Annotated[str, typer.Argument(help="Account name: the ID in coordinator:ID.", show_default=False)],
    workspace: WorkspaceOption,
    password_stdin: Annotated[
        bool, typer.Option("--password-stdin", help="Read the password from the first line of standard input (for scripts), instead of asking twice.")
    ] = False,
) -> None:
    """Create a coordinator account; the password is asked for twice and stored only as a salted scrypt hash."""
    from corla_observer.web.accounts import MIN_PASSWORD_LENGTH, AccountError, AccountStore

    if password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = typer.prompt(
            f"Password for {name} (at least {MIN_PASSWORD_LENGTH} characters)", hide_input=True, confirmation_prompt=True
        )
    store = Workspace.open(workspace)
    try:
        with EventLog.open(store.root) as log:
            account = AccountStore(store.root).add(name, password, log)
    except AccountError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    typer.echo(f"added {account.actor}")


@user_app.command("list")
def user_list(workspace: WorkspaceOption) -> None:
    """List the coordinator accounts."""
    from corla_observer.web.accounts import AccountStore

    for name in AccountStore(workspace).names():
        typer.echo(f"coordinator:{name}")


@app.command("serve")
def serve_command(
    workspace: WorkspaceOption,
    host: Annotated[str, typer.Option("--host", help="Address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8080,
    path_prefix: Annotated[
        str, typer.Option("--path-prefix", help="Serve everything under this path, such as an unguessable /x1y2z3.")
    ] = "",
    secret_file: Annotated[
        Path | None,
        typer.Option(
            "--secret-file",
            help=(
                "The server secret that signs session cookies, created if missing; kept outside the repository "
                "and the workspace (default: $CORLA_OBSERVER_SECRET_FILE, else ~/.config/corla-observer/server-secret)."
            ),
            show_default=False,
        ),
    ] = None,
    receipt_key_file: Annotated[
        Path | None,
        typer.Option(
            "--receipt-key-file",
            help=(
                "The Ed25519 key that signs observers' receipts, created if missing; kept outside the repository "
                "and the workspace (default: $CORLA_OBSERVER_RECEIPT_KEY_FILE, else receipt-key.pem next to the "
                "server secret)."
            ),
            show_default=False,
        ),
    ] = None,
    county: Annotated[
        list[str] | None,
        typer.Option("--county", help="List this county on the dashboard before anything arrives (repeatable).", show_default=False),
    ] = None,
    forwarded_allow_ips: Annotated[
        str, typer.Option("--forwarded-allow-ips", help="Proxies whose X-Forwarded-For is trusted for client addresses.")
    ] = "127.0.0.1",
    public_check: Annotated[
        bool,
        typer.Option(
            "--public-check",
            help="Let anyone check a printed page at /check, without a login or a link (only after the audit: the pages' content is kept from the audit boards during it).",
        ),
    ] = False,
) -> None:
    """Run the web interface: the public dashboard and the coordinator pages."""
    import uvicorn

    from corla_observer.web.app import AppConfig, check_prefix, create_app
    from corla_observer.receipts import default_receipt_key_path, load_receipt_key
    from corla_observer.web.sessions import SecretError, default_secret_path, load_secret

    try:
        prefix = check_prefix(path_prefix)
        secret_path = secret_file or default_secret_path()
        secret = load_secret(secret_path, workspace=workspace)
        receipt_key = load_receipt_key(receipt_key_file or default_receipt_key_path(secret_path), workspace=workspace)
    except (ValueError, SecretError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    typer.echo(f"receipt key {receipt_key.key_id}", err=True)
    web = create_app(
        AppConfig(
            workspace=workspace, secret=secret, receipt_key=receipt_key, path_prefix=prefix, counties=tuple(county or ()),
            public_check=public_check,
        )
    )
    typer.echo(f"serving on http://{host}:{port}{prefix}/", err=True)
    uvicorn.run(
        web,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
        server_header=False,
        timeout_keep_alive=5,
    )


# ---------------------------------------------------------------------------
# Checking an observer's device-held record (corla_observer.observer_record).


@app.command("check-record")
def check_record_command(
    record: Annotated[Path, typer.Argument(help="The record downloaded from the observer page (JSON).", show_default=False)],
    events: Annotated[Path, typer.Argument(help="The published event log, events.jsonl.", show_default=False)],
    server_key: Annotated[
        str | None,
        typer.Option(
            "--server-key",
            help=(
                "The server's receipt key, from a source other than the log: a file holding its base64, PEM or the "
                "JSON served at /receipt-key, or the base64 itself."
            ),
            show_default=False,
        ),
    ] = None,
    anchor: Annotated[
        list[str] | None,
        typer.Option("--anchor", help="A published chain head, SEQ:HASH, that the log must agree with (repeatable).", show_default=False),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
) -> None:
    """Check an observer's device-held record against a published event log (docs/RECEIPTS.md).

    Exits 0 if nothing is wrong, 1 if any problem is found, 2 if the files can't be read.
    """
    from corla_observer.observer_record import RecordError, check_record, load_record, render_report
    from corla_observer.receipts import PayloadError, parse_server_public_key

    try:
        loaded = load_record(record)
        key: bytes | None = None
        if server_key is not None:
            path = Path(server_key)
            key = parse_server_public_key(path.read_text(encoding="utf-8") if path.is_file() else server_key)
        anchors: list[tuple[int, str]] = []
        for item in anchor or ():
            seq_text, _, digest = item.partition(":")
            if not seq_text.isdigit() or len(digest) != 64:
                raise RecordError(f"--anchor must be SEQ:HASH with a 64-digit hash: {item!r}")
            anchors.append((int(seq_text), digest.lower()))
        report = check_record(loaded, events, server_key=key, anchors=anchors)
    except (OSError, RecordError, PayloadError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    typer.echo(json.dumps(report.to_dict(), indent=1) if as_json else render_report(report))
    raise typer.Exit(0 if report.ok else 1)


# ---------------------------------------------------------------------------
# Practice observation (corla_observer.practice): extracts and links.

practice_app = typer.Typer(help="Practice live observation on a rehearsal of the July 2026 primary.", no_args_is_help=True)
app.add_typer(practice_app, name="practice")


@practice_app.command("build")
def practice_build(
    workspace: WorkspaceOption,
    county: Annotated[list[str], typer.Option("--county", help="County to build an extract for (repeatable).", show_default=False)],
    boards: Annotated[int, typer.Option("--boards", help="Audit boards to split each county's ballots among.")] = 1,
    mirror: Annotated[
        Path | None,
        typer.Option(
            "--mirror",
            help="A clone of the public audit center mirror, used to rehearse the July 2026 primary first if the workspace is new (default: $CORLA_OBSERVER_MIRROR).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Build practice extracts: each county's predicted ballots in pull order, with their expected votes from its redacted CVR."""
    from corla_observer.practice import PracticeError, build_extract

    if not (workspace / "events.sqlite3").exists():
        mirror = mirror or (Path(os.environ["CORLA_OBSERVER_MIRROR"]) if os.environ.get("CORLA_OBSERVER_MIRROR") else None)
        if mirror is None:
            typer.echo("error: a new workspace needs --mirror (or CORLA_OBSERVER_MIRROR) to rehearse the primary first", err=True)
            raise typer.Exit(2)
        typer.echo(f"rehearsing the July 2026 primary into {workspace} ...", err=True)
        try:
            run_all(workspace, mirror)
        except (RehearsalError, PredictionError) as exc:
            raise _rehearsal_error(exc) from None
    store = Workspace.open(workspace)
    with EventLog.open(store.root) as log:
        for name in county:
            try:
                extract, sha, _ = build_extract(store, log, name, boards=boards)
            except PracticeError as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(2) from None
            ballots = extract["ballots"]
            per_board = ", ".join(
                f"board {b}: {sum(1 for x in ballots if x['board'] == b)}" for b in range(1, boards + 1)
            )
            typer.echo(
                f"{extract['county']}: {len(ballots)} ballots ({per_board}); "
                f"{sum(1 for x in ballots if x['status'] == 'redacted')} redacted, {sum(1 for x in ballots if x['status'] == 'not_in_cvr')} not in the CVR; "
                f"extract {sha[:16]}"
            )


@practice_app.command("link")
def practice_link(
    workspace: WorkspaceOption,
    county: Annotated[str, typer.Option("--county", help="The county.", show_default=False)],
    board: Annotated[int, typer.Option("--board", help="The audit board.")] = 1,
    observer: Annotated[str | None, typer.Option("--observer", help="An observer's name: makes an entry link.", show_default=False)] = None,
    reader: Annotated[str | None, typer.Option("--reader", help="A practice reader's name: makes a reader link.", show_default=False)] = None,
    base_url: Annotated[
        str, typer.Option("--base-url", help="The site's address, with its path prefix, such as https://example.org/x1y2z3.")
    ] = "http://127.0.0.1:8080",
    actor: Annotated[str, typer.Option("--actor", help="Who makes the link, as coordinator:ID.")] = "system",
) -> None:
    """Make a link for one observer (or practice reader) at one county's board, and print its URL."""
    from corla_observer.practice import LinkStore, PracticeError

    if (observer is None) == (reader is None):
        typer.echo("error: give exactly one of --observer NAME or --reader NAME", err=True)
        raise typer.Exit(2)
    role, name = ("observer", observer) if observer is not None else ("reader", reader)
    assert name is not None
    store = Workspace.open(workspace)
    try:
        with EventLog.open(store.root) as log:
            token, link = LinkStore(store.root).create(role, name, county, board, log, actor=actor)
    except PracticeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    path = "observe" if role == "observer" else "read"
    typer.echo(f"{base_url.rstrip('/')}/{path}/{token}")
    typer.echo(f"({role} {link.actor}, {link.county} board {link.board})", err=True)


@practice_app.command("test-county")
def practice_test_county(
    out: Annotated[Path, typer.Option("--out", help="Directory to write the files into.", show_default=False)],
    seed: Annotated[int, typer.Option("--seed", help="Random seed for the votes.")] = 20201103,
) -> None:
    """Write a fictional county's files (Testland: a manifest, a redacted CVR with random votes, a pull list) with 60-contest ballots.

    Upload them with `upload --county Testland`, then `extract build --county Testland` and
    `practice link --county Testland`. See corla_observer.testcounty.
    """
    from corla_observer.testcounty import COUNTY, generate, make, summary

    paths = generate(out, seed)
    for line in summary(make(seed)):
        typer.echo(line)
    for path in paths.values():
        typer.echo(str(path))
    typer.echo(
        f"next: corla-observer upload --workspace DIR --actor coordinator:ID --county {COUNTY} {' '.join(str(p) for p in paths.values())}",
        err=True,
    )


# ---------------------------------------------------------------------------
# Timestamping the evidence (corla_observer.timestamp).


def _echo_step(name: str, status: str, detail: str) -> None:
    typer.echo(f"  {name}: {status.upper()}" + (f" ({detail})" if detail else ""), err=status in ("failed", "skipped"))


@app.command("commit-evidence")
def commit_evidence_command(
    workspace: WorkspaceOption,
    election: Annotated[str, typer.Option("--election", help="The election, such as 2026/general.", show_default=False)],
    out_repo: Annotated[
        Path, typer.Option("--out-repo", help="A git checkout to commit the SUMS file and proofs to, such as a clone of the public mirror.", show_default=False)
    ],
    subdir: Annotated[
        str | None,
        typer.Option("--subdir", help="Directory within --out-repo (default: ELECTION/observerfiles/evidence).", show_default=False),
    ] = None,
    actor: Annotated[str, typer.Option("--actor", help="Who is responsible, as coordinator:ID.")] = "system",
    log_path: LogOption = None,
    no_push: Annotated[bool, typer.Option("--no-push", help="Commit, but do not push.")] = False,
    include_files: Annotated[
        bool, typer.Option("--include-files", help="Also copy the evidence files themselves (never a CVR without a passing redaction check).")
    ] = False,
    no_ots: Annotated[bool, typer.Option("--no-ots", help="Do not stamp with OpenTimestamps.")] = False,
    no_cosign: Annotated[bool, typer.Option("--no-cosign", help="Do not sign with cosign.")] = False,
    cosign_key: Annotated[
        Path | None,
        typer.Option("--cosign-key", help="Sign with this cosign key file instead of keyless Sigstore signing.", show_default=False),
    ] = None,
    timeout: Annotated[int, typer.Option("--tool-timeout", min=1, help="Seconds allowed for each ots or cosign run.")] = 300,
) -> None:
    """Write a new versioned SHA256SUMS file of the evidence, stamp, sign, commit and push it, and record it.

    Exits 1 if a step failed (a missing tool is reported as skipped, not failed), 2 if nothing was committed.
    """
    from corla_observer.timestamp import CosignOptions, TimestampError, commit_evidence

    store = Workspace.open(workspace)
    try:
        with EventLog.open(log_path or store.root) as log:
            result = commit_evidence(
                store,
                log,
                election=election,
                out_repo=out_repo,
                subdir=subdir,
                actor=actor,
                push=not no_push,
                include_files=include_files,
                ots=not no_ots,
                cosign=CosignOptions(enabled=not no_cosign, key=cosign_key),
                timeout=timeout,
                echo=lambda line: typer.echo(f"    {line}", err=True),
            )
    except (TimestampError, EventLogError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    seq, head = result.evidence.log_head
    where = f"{result.subdir}/{result.sums_name}" if result.subdir else result.sums_name
    typer.echo(f"{where}: version {result.version}, {len(result.evidence.files)} files, sha256 {result.sums_sha256}")
    typer.echo(f"  event log head: {seq} {head}")
    typer.echo(f"  software: {result.evidence.software}")
    for note in result.evidence.notes:
        typer.echo(f"  note: {note}")
    for step in result.steps:
        _echo_step(step.name, step.status, step.detail)
    typer.echo(f"commit {result.commit}" + (" (pushed)" if result.pushed else " (not pushed)"))
    typer.echo(f"recorded as event {result.event_seq}")
    if result.failed:
        raise typer.Exit(1)


@app.command("upgrade-proofs")
def upgrade_proofs_command(
    out_repo: Annotated[Path, typer.Option("--out-repo", help="The git checkout holding the .ots proofs.", show_default=False)],
    workspace: WorkspaceOption,
    subdir: Annotated[str | None, typer.Option("--subdir", help="Only proofs under this directory.", show_default=False)] = None,
    actor: Annotated[str, typer.Option("--actor", help="Who is responsible, as coordinator:ID.")] = "system",
    log_path: LogOption = None,
    no_push: Annotated[bool, typer.Option("--no-push", help="Commit, but do not push.")] = False,
    include_complete: Annotated[
        bool, typer.Option("--all", help="Also try proofs that already have a Bitcoin attestation.")
    ] = False,
    timeout: Annotated[int, typer.Option("--tool-timeout", min=1, help="Seconds allowed for each ots run.")] = 300,
) -> None:
    """Run `ots upgrade` on pending OpenTimestamps proofs, commit the upgraded ones and record it.

    Exits 1 if ots is not installed or an upgrade failed.
    """
    from corla_observer.timestamp import TimestampError, upgrade_proofs

    store = Workspace.open(workspace)
    try:
        with EventLog.open(log_path or store.root) as log:
            result = upgrade_proofs(
                log,
                out_repo=out_repo,
                subdir=subdir,
                actor=actor,
                push=not no_push,
                include_complete=include_complete,
                timeout=timeout,
                echo=lambda line: typer.echo(f"    {line}", err=True),
            )
    except (TimestampError, EventLogError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    if result.skipped:
        typer.echo(f"SKIPPED: {result.skipped}", err=True)
    for label, paths in (
        ("upgraded", result.upgraded),
        ("still pending", result.still_pending),
        ("already complete", result.complete),
        ("failed", result.failed),
    ):
        for path in paths:
            typer.echo(f"{label}: {path}")
    for step in result.steps:
        _echo_step(step.name, step.status, step.detail)
    if result.commit:
        typer.echo(f"commit {result.commit}" + (" (pushed)" if result.pushed else " (not pushed)"))
    else:
        typer.echo("nothing to commit")
    typer.echo(f"recorded as event {result.event_seq}")
    if result.skipped or result.failed:
        raise typer.Exit(1)


@app.command("verify-evidence")
def verify_evidence_command(
    sums: Annotated[Path, typer.Argument(help="A SHA256SUMS*.txt file.", show_default=False)],
    base: Annotated[
        Path | None, typer.Option("--base", help="Directory the listed paths are relative to (default: the SUMS file's).", show_default=False)
    ] = None,
    workspace: Annotated[
        Path | None, typer.Option("--workspace", help="Also look files up by hash in this workspace.", show_default=False)
    ] = None,
    cosign_key: Annotated[
        Path | None, typer.Option("--cosign-key", help="Public key, for a bundle signed with a key file.", show_default=False)
    ] = None,
    certificate_identity: Annotated[
        str | None, typer.Option("--certificate-identity", help="Expected signer identity, for keyless signing.", show_default=False)
    ] = None,
    certificate_oidc_issuer: Annotated[
        str | None, typer.Option("--certificate-oidc-issuer", help="Expected OIDC issuer, for keyless signing.", show_default=False)
    ] = None,
    quiet: Annotated[bool, typer.Option("--quiet", help="Do not list each file that checks out.")] = False,
) -> None:
    """Recompute the listed files' hashes and check the OpenTimestamps and Sigstore proofs.

    Exits 1 if a file differs, a line is malformed or a proof fails; files not available locally are only counted.
    """
    from corla_observer.timestamp import verify_evidence

    if not sums.is_file():
        typer.echo(f"error: no such file: {sums}", err=True)
        raise typer.Exit(2)
    report = verify_evidence(
        sums,
        base=base,
        workspace=Workspace(workspace) if workspace is not None else None,
        cosign_key=cosign_key,
        certificate_identity=certificate_identity,
        certificate_oidc_issuer=certificate_oidc_issuer,
    )
    for key in ("version", "time", "event-log-head", "software"):
        if key in report.header:
            value = report.header[key]
            typer.echo(f"{key}: {colorado_time(value) + ' (' + value + ')' if key == 'time' else value}")
    for check in report.checks:
        if quiet and check.status == "ok" and not check.subject.endswith((".ots", ".cosign-bundle")):
            continue
        typer.echo(f"{check.status.upper():9} {check.subject}" + (f"  {check.detail}" if check.detail else ""))
    files = [c for c in report.checks[1:] if not c.subject.endswith((".ots", ".cosign-bundle"))]
    typer.echo(
        f"files: {sum(c.status == 'ok' for c in files)} ok, {sum(c.status == 'mismatch' for c in files)} differ, "
        f"{sum(c.status == 'missing' for c in files)} not available locally"
    )
    if not report.ok:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Seed entry by hand (corla_observer.seeds), and the extract (corla_observer.extract).


def _seed_value(seed: str | None) -> str:
    if seed is not None:
        return seed
    value: str = typer.prompt("Seed (20 digits)")
    return value


@seed_app.command("set")
def seed_set_command(
    workspace: WorkspaceOption,
    actor: ActorOption,
    source: Annotated[str, typer.Option("--source", help="Where the seed was announced or read.", show_default=False)],
    seed: Annotated[str | None, typer.Argument(help="The 20-digit seed (asked for if omitted).", show_default=False)] = None,
) -> None:
    """Enter the seed, in effect at once. Entering a different value later is flagged as a change."""
    from corla_observer.seeds import SeedError, enter_seed

    with EventLog.open(workspace) as log:
        try:
            seq = enter_seed(log, _seed_value(seed), actor, source)
        except (SeedError, ValueError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from None
    typer.echo(f"seed entered and in effect (event {seq}); another coordinator may check it with `seed confirm`")


@seed_app.command("confirm")
def seed_confirm_command(
    workspace: WorkspaceOption,
    actor: ActorOption,
    seed: Annotated[str | None, typer.Argument(help="The 20-digit seed, entered independently (asked for if omitted).", show_default=False)] = None,
    source: Annotated[str, typer.Option("--source", help="Where you got the seed.")] = "",
) -> None:
    """Check the seed with an optional second, independent entry. Exits 1 if it differs from the seed in effect.

    The coordinator who made the entry in effect is recorded as entering it again (a re-entry, in effect).
    """
    from corla_observer.seeds import SeedError, confirm_seed

    with EventLog.open(workspace) as log:
        try:
            outcome = confirm_seed(log, _seed_value(seed), actor, source)
        except (SeedError, ValueError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from None
    if outcome.reentry:
        if outcome.matched:
            typer.echo(f"seed {outcome.second.seed} entered again by {actor} (event {outcome.seq}), unchanged")
            return
        typer.echo(
            f"CHANGED (event {outcome.seq}): {actor} entered {outcome.second.seed}, replacing {outcome.first.seed}; "
            "it is in effect, and flagged",
            err=True,
        )
        raise typer.Exit(1)
    if not outcome.matched:
        typer.echo(
            f"MISMATCH (event {outcome.seq}): {outcome.first.actor} entered {outcome.first.seed}, {actor} entered "
            f"{outcome.second.seed}. Check the announced seed and enter the right one with `seed set`.",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(f"seed {outcome.first.seed} confirmed (event {outcome.seq}): entered by {outcome.first.actor}, confirmed by {actor}")


@seed_app.command("status")
def seed_status_command(workspace: WorkspaceOption) -> None:
    """Show the seed in effect, whether a second entry matched it, and any mismatches and changes."""
    from corla_observer.seeds import describe_state, seed_state

    with EventLog.open(workspace, read_only=True) as log:
        for line in describe_state(seed_state(log.iter_events())):
            typer.echo(line)


extract_app = typer.Typer(
    help="The observers' extract: each county's ballots in pull order with their expected votes (docs/EXTRACT.md).",
    no_args_is_help=True,
)
app.add_typer(extract_app, name="extract")


@extract_app.command("build")
def extract_build_command(
    workspace: WorkspaceOption,
    county: Annotated[list[str], typer.Option("--county", help="County to build the extract for (repeatable).", show_default=False)],
    actor: ActorOption,
    boards: Annotated[
        int | None,
        typer.Option("--boards", help="Audit boards to split a predicted order among (default: the recorded count).", show_default=False),
    ] = None,
    sample_count: Annotated[
        int | None,
        typer.Option(
            "--sample-count",
            help="A test county's fixed sample count, when its selections are predicted because no pull list was uploaded (default 50).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Build and record each county's extract, from its pull list (or CORLA's posted or the predicted selections) and redacted CVR."""
    from corla_observer.extract import ExtractError, build_extract

    store = Workspace.open(workspace)
    with EventLog.open(store.root) as log:
        datamap = build_map(log, store)
        for name in county:
            try:
                built = build_extract(store, log, name, boards=boards, actor=actor, datamap=datamap, sample_count=sample_count)
            except (ExtractError, ValueError) as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(2) from None
            _print_extract(built.extract, built.sha256)
            typer.echo(f"  recorded as event {built.seq}; page index {built.page_index_sha256}")
            for layout, by_board in built.pdfs.items():
                for number, pdf in by_board.items():
                    typer.echo(f"  PDF board {number} {layout}: {pdf}")


def _print_extract(extract: dict[str, object], sha: str) -> None:
    from corla_observer.extract import layouts, page_hashes

    ballots = extract["ballots"]
    assert isinstance(ballots, list)
    labels = {"predicted": "PREDICTED ORDER (no pull list yet)", "audit_center": "AUDIT CENTER ORDER (CORLA's posted selections; no pull list yet)", "targeted": "TARGETED (not part of the RLA sample)"}
    order = labels.get(str(extract["order"]), "pull list order")
    distinct = len({b["position"] for b in ballots if isinstance(b, dict)})
    possible = len({b["position"] for b in ballots if isinstance(b, dict) and not b.get("certain", True)})
    boundary = len({b["position"] for b in ballots if isinstance(b, dict) and b.get("boundary")})
    typer.echo(f"{extract['county']}: {distinct} ballots, {order}; extract {sha}")
    if possible or boundary:
        typer.echo(f"  {possible} POSSIBLE (beyond the low estimate), {boundary} BOUNDARY (listed on two boards)")
    typer.echo(f"  seed {extract['seed'] or 'none'}")
    inputs = extract["inputs"]
    assert isinstance(inputs, dict)
    for name, value in inputs.items():
        if name != "seed":
            typer.echo(f"  {name}: {value or 'none'}")
    checks = extract["checks"]
    assert isinstance(checks, dict)
    for name, ids in checks.items():
        if isinstance(ids, dict):
            for key in ("not_listed", "not_in_other"):
                if ids.get(key):
                    typer.echo(f"  {ids['source']} {key.replace('_', ' ')}: {', '.join(ids[key][:20])}")
        elif isinstance(ids, list) and ids:
            typer.echo(f"  {name.replace('_', ' ')}: {', '.join(ids[:20])}{' ...' if len(ids) > 20 else ''}")
        elif ids is True:
            typer.echo(f"  {name.replace('_', ' ').upper()}")
    by_layout = {layout: page_hashes(extract, sha, layout) for layout in layouts(extract)}
    for board in sorted({b for hashes in by_layout.values() for b in hashes}):
        count = sum(1 for b in ballots if isinstance(b, dict) and b["board"] == board)
        sheets = ", ".join(f"{len(hashes[board])} {layout}" for layout, hashes in by_layout.items())
        typer.echo(f"  board {board}: {count} ballots; pages: {sheets}")


@extract_app.command("show")
def extract_show_command(
    workspace: WorkspaceOption,
    county: CountyOption,
    pages: Annotated[bool, typer.Option("--pages", help="List every page's hash.")] = False,
) -> None:
    """Show a county's latest extract: its hash, order, inputs, and pages per board."""
    from corla_observer.extract import latest_extract, page_index

    name = canonical_county(county) or county
    store = Workspace.open(workspace)
    with EventLog.open(store.root, read_only=True) as log:
        found = latest_extract(store, log, name)
    if found is None:
        typer.echo(f"error: no extract for {name}", err=True)
        raise typer.Exit(1)
    extract, sha = found
    _print_extract(extract, sha)
    if pages:
        index = page_index(extract, sha)
        by_layout = index["layouts"] if "layouts" in index else {"portrait": index}
        for layout, part in by_layout.items():
            suffix = "" if layout == "portrait" else f" ({layout})"
            for board, entries in part["boards"].items():
                for entry in entries:
                    typer.echo(
                        f"  board {board} page {entry['page']}{suffix}: {entry['sha256']} "
                        f"(ballots {entry['positions'][0]} to {entry['positions'][-1]})"
                    )


@extract_app.command("print")
def extract_print_command(
    workspace: WorkspaceOption,
    county: CountyOption,
    board: Annotated[int | None, typer.Option("--board", help="The audit board (all boards for CSV if omitted).", show_default=False)] = None,
    form: Annotated[str, typer.Option("--format", help="pdf, text or csv.")] = "text",
    layout: Annotated[
        str | None,
        typer.Option("--layout", help="For pdf: portrait-2 (the default: portrait, two columns), portrait-1, landscape-2 or landscape-3.", show_default=False),
    ] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Write here instead of standard output (needed for pdf).", show_default=False)] = None,
) -> None:
    """Print a board's extract: the printout as a PDF (the one recorded when it was built), plain text, or CSV."""
    from corla_observer.extract import ExtractError, default_layout, extract_pdfs, extract_software, latest_extract, layouts, render_text, to_csv
    from hashlib import sha256

    from corla_observer.pdfextract import printout

    name = canonical_county(county) or county
    store = Workspace.open(workspace)
    with EventLog.open(store.root, read_only=True) as log:
        found = latest_extract(store, log, name)
        if found is None:
            typer.echo(f"error: no extract for {name}", err=True)
            raise typer.Exit(1)
        extract, sha = found
        software = extract_software(log, sha)
        body: bytes | None = None
        text = ""
        try:
            if form == "csv":
                text = to_csv(extract, sha, board, extract_pdfs(log, sha))
            elif board is None:
                raise ExtractError("--board is needed for pdf and text")
            elif form == "text":
                text = render_text(extract, sha, board, software=software)
            elif form == "pdf":
                chosen = layout or default_layout(extract)
                if chosen not in layouts(extract):
                    raise ExtractError(f"--layout is one of {', '.join(layouts(extract))}")
                if out is None:
                    raise ExtractError("--out is needed for pdf")
                body = printout(store, log, extract, sha, board, chosen)
            else:
                raise ExtractError("--format is pdf, text or csv")
        except ExtractError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from None
    if body is not None and out is not None:
        out.write_bytes(body)
        typer.echo(f"wrote {out} (SHA-256 {sha256(body).hexdigest()})", err=True)
    elif out is not None:
        out.write_text(text, encoding="utf-8", newline="")
        typer.echo(f"wrote {out}", err=True)
    else:
        typer.echo(text, nl=False)


# ---------------------------------------------------------------------------
# Running an election live (corla_observer.cli_live)

from corla_observer.cli_live import register as _register_live  # noqa: E402
from corla_observer.cli_live import user_copy as _user_copy  # noqa: E402

_register_live(app)
user_app.command("copy")(_user_copy)

from corla_observer.cli_photo import register as _register_photo  # noqa: E402

_register_photo(app)


# ---------------------------------------------------------------------------
# Targeted ballots (corla_observer.targets, docs/TARGETING.md), as on /coord/targets.

targets_app = typer.Typer(help="Targeted ballots: patterns of votes, random draws, the working list, and sending it to the county.", no_args_is_help=True)
app.add_typer(targets_app, name="targets")


def _targets_county(store: Workspace, log: EventLog, county: str) -> tuple[str, tuple[str, Any], Any]:
    from corla_observer import targets as tg
    from corla_observer.classify import known_county
    from corla_observer.extract import county_manifest
    from corla_observer.uploads import status as upload_status

    name = known_county(county)
    entry = upload_status(log, [name]).get(name) if name else None
    received = entry.latest.get("redacted_cvr") if entry else None
    if name is None or received is None:
        typer.echo(f"error: no redacted CVR from {county!r}", err=True)
        raise typer.Exit(2)
    votes = tg.load_votes(store.path_for(received.sha256))
    return name, (received.sha256, votes), county_manifest(store, log, name)


@targets_app.command("draw")
def targets_draw(
    workspace: WorkspaceOption,
    county: Annotated[str, typer.Option("--county", help="The county.", show_default=False)],
    actor: ActorOption,
    require: Annotated[
        list[str], typer.Option("--require", help='"Contest=Candidate", names as in the CVR (repeatable).', show_default=False)
    ],
    seed: Annotated[str, typer.Option("--seed", help="The seed for the draw: any text.", show_default=False)],
    size: Annotated[int, typer.Option("--size", help="Ballots to draw.")] = 20,
) -> None:
    """Draw from a pattern of votes into the county's working list (skipping ballots already listed or sampled), and print the draw as for an email."""
    from corla_observer import targets as tg

    store = Workspace.open(workspace)
    with EventLog.open(store.root) as log:
        county_name, (cvr_sha, votes), manifest = _targets_county(store, log, county)
        requirement: dict[int, list[int]] = {}
        for item in require:
            contest, _, choice = item.partition("=")
            cv = votes.contest_by_name(contest.strip())
            if cv is None or choice.strip() not in cv.choices:
                typer.echo(f"error: no {item!r} in the CVR's contests", err=True)
                raise typer.Exit(2)
            requirement.setdefault(cv.index, []).append(cv.choices.index(choice.strip()))
        try:
            pattern = tg.pattern_of(county_name, tg.pattern_definition(requirement, votes))
            found = tg.sources(store, log, county_name)
            record = tg.draw(
                votes, county=county_name, cvr_sha256=cvr_sha, pattern=pattern, seed=seed, seed_source="entered for this draw",
                number=tg.draw_count(log, county_name) + 1, locations=manifest.locations, manifest_sha256=manifest.sha256, actor=actor,
                size=size, exclude=tg.holders(store, log, county_name, found),
            )  # fmt: skip
            record, sha = tg.record_draw(store, log, record)
        except tg.DrawError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from None
    typer.echo(tg.draw_email(record, sha), nl=False)


@targets_app.command("add")
def targets_add(
    workspace: WorkspaceOption,
    county: Annotated[str, typer.Option("--county", help="The county.", show_default=False)],
    actor: ActorOption,
    ids: Annotated[list[str], typer.Argument(help="Imprinted IDs.", show_default=False)],
    note: Annotated[str, typer.Option("--note", help="Why these ballots (optional).")] = "",
) -> None:
    """Add ballots to the working list by imprinted ID, as one source, each looked up in the CVR and the manifest (one in neither is not added)."""
    from corla_observer import targets as tg

    store = Workspace.open(workspace)
    with EventLog.open(store.root) as log:
        county_name, (_, votes), manifest = _targets_county(store, log, county)
        found = []
        for text in ids:
            normalized = tg.normalize_id(text)
            entry = tg.lookup(normalized, votes, manifest.locations, manifest.sizes) if normalized else None
            if entry is None or not entry.found:
                typer.echo(f"{text}: not found, not added")
                continue
            typer.echo(f"{entry.imprinted_id}: {entry.location or 'no location'}{'' if entry.cvr_status else ', not in the CVR'}")
            found.append(entry.imprinted_id)
        try:
            seq = tg.enter_ids(store, log, county_name, found, actor, note=note) if found else None
        except tg.DrawError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from None
    typer.echo(f"{len(found)} added" + (f" (source: entry {seq})" if seq is not None else ""))


@targets_app.command("include")
def targets_include(
    workspace: WorkspaceOption,
    county: Annotated[str, typer.Option("--county", help="The county.", show_default=False)],
    actor: ActorOption,
    source: Annotated[str, typer.Argument(help='A source: "draw 3" or "entry 25".', show_default=False)],
    exclude: Annotated[bool, typer.Option("--exclude", help="Exclude it from the working list instead.")] = False,
) -> None:
    """Include a source in the county's working list, or exclude it (kept on record)."""
    from corla_observer import targets as tg

    store = Workspace.open(workspace)
    with EventLog.open(store.root) as log:
        try:
            tg.set_included(log, known_county(county) or county, source, not exclude, actor)
        except tg.DrawError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from None
    typer.echo(f"{source}: {'excluded' if exclude else 'included'}")


@targets_app.command("list")
def targets_list(
    workspace: WorkspaceOption,
    county: Annotated[str, typer.Option("--county", help="The county.", show_default=False)],
) -> None:
    """Print the county's sources (sent or in the working list) and the working list as for an email."""
    from datetime import UTC, datetime

    from corla_observer import targets as tg

    store = Workspace.open(workspace)
    with EventLog.open(store.root) as log:
        county_name, (_, votes), manifest = _targets_county(store, log, county)
        found = tg.sources(store, log, county_name)
        for s in found:
            where = f"sent in Targeted list {s.sent_in}" if s.sent_in else ("included" if s.included else "excluded")
            typer.echo(f"{s.id}: {s.title}, {len(s.ids)} ballots, {where}: {s.description}")
        entries = tg.working_list(found, votes, manifest.locations, manifest.sizes, tg.rla_ids(store, log, county_name))
    typer.echo(tg.list_email(county_name, "Working targeted list (not yet sent)", entries, datetime.now(UTC).strftime("%Y-%m-%d")), nl=False)


@targets_app.command("send")
def targets_send(
    workspace: WorkspaceOption,
    county: Annotated[str, typer.Option("--county", help="The county.", show_default=False)],
    actor: ActorOption,
    boards: Annotated[int, typer.Option("--boards", help="Audit boards to split the list among.")] = 1,
) -> None:
    """Send the working list to the county as the next Targeted list: its extract (one of the county's pull lists beside its RLA sample) and its text for an email."""
    from corla_observer import targets as tg
    from corla_observer.extract import ExtractError

    store = Workspace.open(workspace)
    with EventLog.open(store.root) as log:
        county_name, (_, votes), manifest = _targets_county(store, log, county)
        try:
            sent = tg.send(store, log, county_name, actor, votes=votes, locations=manifest.locations, sizes=manifest.sizes, boards=boards)
        except (ExtractError, tg.DrawError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from None
        typer.echo(tg.list_email(county_name, sent.name, sent.entries, "sent today", [f"Extract {sent.extract_sha256[:12]}; record {sent.record_sha256[:16]}"]), nl=False)
        typer.echo(f"  {sent.name} ({sent.key}): extract {sent.extract_sha256}; recorded as event {sent.seq}")
