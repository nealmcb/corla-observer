"""The ``photo`` commands: checking photos or scans of printed extract pages (`corla_observer.photocheck`, docs/PHOTO_CHECK.md).

Registered on the main command line (`corla_observer.cli`) by `register`:

``photo check IMAGE... --workspace W [--actor ACTOR] [--json] [--no-record] [--require-committed]``
    Check each image against the workspace's extracts and print which page
    it is, how it was located, its content verdict with the counts of lines
    matched, differing and unread, its commitment status (the commitments
    covering it with their proofs, or NOT YET COMMITTED), and every line
    that differs or could not be read, with what was read of it in each
    pass and why it was not accepted (in ``--json``, each line's
    ``reason`` and ``reads``). Each check is recorded as a
    ``photo.checked`` event with the photo stored, without its metadata,
    under its hash (``--no-record``: neither).

    Exit status, by the content verdict: 0 if every image is MATCH; 1 if
    any is MISMATCH or UNKNOWN PAGE (or could not be read as an image);
    otherwise 2 (some MATCH WITH UNREAD LINES). With
    ``--require-committed``, a page no commitment covers yet exits 1 even if
    it matches. 3 if the check could not run at all (a missing tool or
    workspace).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from corla_observer.events import EventLog, InvalidEventError
from corla_observer.photocheck import UNREAD_MEANING, CheckResult, Commitment, PageFinder, check_photo, record_check
from corla_observer.photoimage import PhotoError, ToolMissingError, load_photo, missing_tools
from corla_observer.workspace import Workspace

photo_app = typer.Typer(help="Check photos or scans of printed extract pages against the committed extract (docs/PHOTO_CHECK.md).", no_args_is_help=True)

#: The command's exit status when the check could not run at all.
EXIT_CANNOT_RUN = 3


def describe_commitment(c: Commitment) -> str:
    """One commitment and its proofs, in a line."""
    if c.ots == "bitcoin":
        ots = f"Bitcoin block {c.bitcoin_block}" if c.bitcoin_block is not None else "OpenTimestamps proof in a Bitcoin block"
    else:
        ots = {"pending": "OpenTimestamps proof pending (not yet in a Bitcoin block)", "skipped": "no OpenTimestamps proof", "failed": "OpenTimestamps failed"}.get(c.ots, "no OpenTimestamps proof")
    sigstore = f"Sigstore Rekor entry {c.rekor_log_index}" if c.rekor_log_index is not None else ("Sigstore signature" if c.sigstore == "done" else "no Sigstore signature")
    commit = f"commit {c.commit[:12]}" + (" (pushed)" if c.pushed else " (not pushed)") if c.commit else "no commit"
    return f"{c.sums_name} (event {c.seq}, {c.time}, lists the {' and the '.join(c.covers)}): {ots}; {sigstore}; {commit}"


def report_lines(result: CheckResult) -> list[str]:
    """The human-readable report of one check, after its summary line."""
    out: list[str] = []
    loc = result.location
    if loc is not None:
        state = "the county's latest" if loc.extract.latest else "SUPERSEDED, not the county's latest"
        out.append(f"which page: {loc.label}")
        out.append(f"located {result.located_text()}")
        out.append(f"page {loc.sha256}")
        out.append(f"extract {loc.extract.sha256} ({loc.extract.purpose}, {state})")
    elif result.extract_sha256:
        out.append(f"extract {result.extract_sha256}, located {result.located_text()}")
    if result.lines:
        counts = result.counts()
        out.append(f"content: {result.verdict}: {counts['matched']} lines matched, {counts['differs']} differ, {counts['unread']} unread")
        if counts["unread"]:
            out.append(f"  ({UNREAD_MEANING})")
    for reason in result.reasons:
        out.append(f"why: {reason}")
    for note in result.notes:
        out.append(f"note: {note}")
    if result.commitment_status is not None:
        out.append(f"commitment: {result.commitment_text()}")
    out += [f"  proofs: {describe_commitment(c)}" for c in result.commitments]
    for line in result.lines:
        where = f"ballot {line.n} ({line.imprinted_id}) " if line.n is not None and line.kind != "id" else (f"ballot {line.n} " if line.n is not None else "")
        if line.status == "differs":
            what = f"expected {line.expected!r}" if line.expected else "not on the committed page"
            detail = f" ({line.detail})" if line.detail else ""
            out.append(f"DIFFERS {where}{line.kind}: printed {line.read!r}, {what}{detail}")
        elif line.status == "unread":
            seen = f"read {line.read!r}" if line.read else "read: nothing found"
            out.append(f"unread  {where}{line.kind}: {line.expected!r}, {seen}{' (' + line.detail + ')' if line.detail else ''}")
        if line.status != "matched":
            if line.reason:
                out.append(f"          why: {line.reason}")
            for r in line.reads:
                out.append(f"          {r['pass']}: {r['text']!r} ({r['judged']})")
    marked = [line for line in result.lines if line.marked]
    if marked:
        out.append(f"marked: {len(marked)} box(es) hold a hand-made mark: " + ", ".join(f"ballot {line.n} {line.contest}" for line in marked[:20]))
    return out


@photo_app.command("check")
def photo_check(
    images: Annotated[list[Path], typer.Argument(help="Photos or scans (JPEG, PNG, WebP or TIFF), one page each.", show_default=False)],
    workspace: Annotated[Path, typer.Option("--workspace", help="Workspace directory.", show_default=False)],
    actor: Annotated[str, typer.Option("--actor", help="Who checks, as coordinator:ID or observer:ID, recorded in the event.")] = "system",
    as_json: Annotated[bool, typer.Option("--json", help="Print the results as JSON.")] = False,
    no_record: Annotated[bool, typer.Option("--no-record", help="Neither store the photos nor record the checks.")] = False,
    require_committed: Annotated[
        bool, typer.Option("--require-committed", help="Exit 1 for a page that matches but no evidence commitment covers yet.")
    ] = False,
) -> None:
    """Check photos or scans of printed extract pages: exit 0 if all MATCH, 1 if any MISMATCH or UNKNOWN PAGE, else 2 (by the content; see --require-committed)."""
    missing = missing_tools()
    if missing:
        typer.echo(f"error: needed but not installed: {', '.join(missing)}", err=True)
        raise typer.Exit(EXIT_CANNOT_RUN)
    if not workspace.is_dir():
        typer.echo(f"error: no workspace at {workspace}", err=True)
        raise typer.Exit(EXIT_CANNOT_RUN)
    store = Workspace.open(workspace)
    codes: list[int] = []
    results: list[dict[str, object]] = []
    with EventLog.open(store.root, read_only=no_record) as log:
        finder = PageFinder(store, log)
        for image in images:
            try:
                photo = load_photo(image.read_bytes())
                result = check_photo(store, log, photo, finder=finder, work_parent=store.root / "staging")
            except (OSError, PhotoError) as exc:
                codes.append(1)
                results.append({"image": str(image), "error": str(exc)})
                if not as_json:
                    typer.echo(f"{image}: ERROR: {exc}")
                continue
            except ToolMissingError as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(EXIT_CANNOT_RUN) from None
            codes.append(result.exit_code_for(require_committed))
            seq = None
            if not no_record:
                try:
                    seq = record_check(store, log, result, photo, actor=actor, via="cli").seq
                except (InvalidEventError, ValueError) as exc:
                    typer.echo(f"error: {exc}", err=True)
                    raise typer.Exit(EXIT_CANNOT_RUN) from None
            results.append({"image": str(image), **result.to_dict(), "event_seq": seq})
            if not as_json:
                typer.echo(f"{image}: {result.summary}")
                for line in report_lines(result):
                    typer.echo(f"  {line}")
                if seq is not None:
                    typer.echo(f"  recorded as event {seq}; photo stored as {result.photo_sha256}")
    if as_json:
        typer.echo(json.dumps(results, indent=1, ensure_ascii=False))
    if 1 in codes:
        raise typer.Exit(1)
    if 2 in codes:
        raise typer.Exit(2)


def register(app: typer.Typer) -> None:
    """Add the ``photo`` command group to ``app``."""
    app.add_typer(photo_app, name="photo")

