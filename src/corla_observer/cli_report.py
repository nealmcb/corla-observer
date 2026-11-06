"""The ``report`` commands: observation reports, compared with each other and with CORLA's (`corla_observer.obsreport`, `corla_observer.reportcheck`).

Registered on the main command line (`corla_observer.cli`) by `register`. Every command takes an
observation report in any of its forms, told apart by content:

- a marked extract spreadsheet (CSV or workbook; ``--board`` if it covers several boards, and
  ``--observer`` to name who marked it);
- a record downloaded from the observer page (``corla-observer-record``; check it against the
  published log first, with ``check-record``);
- the server's signed report of one device (``/observe/TOKEN/report.json``), whose signature is
  checked when ``--server-key`` is given;
- a report as ``report show --json`` prints it.

``report show SOURCE [--json]``
    Print the report.

``report compare A B [--json]``
    Compare two reports of one extract and board, ballot by ballot. Exits 1 if they differ.

``report corla SOURCE DISCREPANCIES [--json]``
    Compare a report with CORLA's discrepancy report (its published file). Exits 1 if any ballot
    differs. For a spreadsheet, CORLA's discrepancies on its listed ballots that were not observed
    are reported too.

Exit status 2: a file cannot be read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from corla_observer.obsreport import MarksError, render_report, report_from_marks, report_from_record

report_app = typer.Typer(help="Observation reports: from a spreadsheet, a record or the server; compared with each other and with CORLA's.", no_args_is_help=True)

BoardOption = Annotated[str | None, typer.Option("--board", help="The board, for a spreadsheet of several boards.", show_default=False)]
ObserverOption = Annotated[str, typer.Option("--observer", help="Who marked a spreadsheet, as the report names them.")]
KeyOption = Annotated[
    str | None,
    typer.Option("--server-key", help="The server's receipt key (base64, PEM, the JSON at /receipt-key, or a file of one), to check a signed report.", show_default=False),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON.")]


class SourceError(ValueError):
    """An observation report's source that cannot be read."""


def load_source(path: Path, *, board: str | None = None, observer: str = "", server_key: str | None = None) -> tuple[dict[str, Any], list[str], str]:
    """A report from any of its forms; the imprinted IDs it lists (for a spreadsheet); and a note on how it was read."""
    data = path.read_bytes()
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        value = None
    if isinstance(value, dict):
        if value.get("format") == "corla-observer-record":
            return report_from_record(value), [], "from a downloaded record (its acknowledged entries)"
        if isinstance(value.get("report"), dict) and "signature" in value:
            body = value["report"]
            note = "the server's signed report; signature not checked (no --server-key)"
            if server_key is not None:
                from cryptography.exceptions import InvalidSignature
                from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

                from corla_observer.events import canonical_json
                from corla_observer.receipts import PayloadError, b64decode, key_id, parse_server_public_key

                key_path = Path(server_key)
                raw = parse_server_public_key(key_path.read_text(encoding="utf-8") if key_path.is_file() else server_key)
                try:
                    if body.get("key_id") != key_id(raw):
                        raise InvalidSignature
                    Ed25519PublicKey.from_public_bytes(raw).verify(b64decode(value["signature"], "signature"), canonical_json(body))
                except (InvalidSignature, PayloadError) as exc:
                    raise SourceError(f"{path}: the report's signature does not check with the server key given") from exc
                note = "the server's signed report; signature checked"
            report = {k: v for k, v in body.items() if k not in ("device_id", "head_seq", "head_hash", "key_id")}
            return report, [], note
        if value.get("type") == "observation_report":
            return value, [], "a report"
        raise SourceError(f"{path}: JSON, but not a record or an observation report")
    try:
        report = report_from_marks(data, observer=observer, board=board)
    except MarksError as exc:
        raise SourceError(f"{path}: {exc}") from None
    from corla_observer.obsreport import _sheet_rows

    rows = _sheet_rows(data)
    header_at = next(i for i, r in enumerate(rows) if r[:1] and r[0].strip().lower() == "board" and "imprinted id" in [c.strip().lower() for c in r])
    head = [c.strip().lower() for c in rows[header_at]]
    b, imp = head.index("board"), head.index("imprinted id")
    listed = [r[imp].strip() for r in rows[header_at + 1 :] if len(r) > imp and r[b].strip() == report["board"]]
    return report, listed, "a marked spreadsheet"


def _load(path: Path, **kwargs: Any) -> tuple[dict[str, Any], list[str], str]:
    try:
        return load_source(path, **kwargs)
    except (OSError, SourceError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None


@report_app.command("show")
def show(source: Path, board: BoardOption = None, observer: ObserverOption = "", server_key: KeyOption = None, as_json: JsonOption = False) -> None:
    """Print an observation report."""
    report, _, note = _load(source, board=board, observer=observer, server_key=server_key)
    typer.echo(json.dumps(report, indent=1, ensure_ascii=False) if as_json else f"({note})\n" + render_report(report))


@report_app.command("compare")
def compare(a: Path, b: Path, board: BoardOption = None, server_key: KeyOption = None, as_json: JsonOption = False) -> None:
    """Compare two observation reports of one extract and board. Exits 1 if they differ."""
    from corla_observer.reportcheck import compare_reports, render_comparison

    left, _, note_a = _load(a, board=board, server_key=server_key)
    right, _, note_b = _load(b, board=board, server_key=server_key)
    try:
        result = compare_reports(left, right)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    if as_json:
        typer.echo(json.dumps(result.to_dict(), indent=1, ensure_ascii=False))
    else:
        typer.echo(f"a: {a} ({note_a})\nb: {b} ({note_b})\n" + render_comparison(result))
    raise typer.Exit(1 if result.differences else 0)


@report_app.command("corla")
def corla(source: Path, discrepancies: Path, board: BoardOption = None, observer: ObserverOption = "", server_key: KeyOption = None, as_json: JsonOption = False) -> None:
    """Compare an observation report with CORLA's discrepancy report. Exits 1 if any ballot differs."""
    from corla_observer.reportcheck import compare_with_corla, read_corla_file, render_comparison

    report, listed, note = _load(source, board=board, observer=observer, server_key=server_key)
    try:
        found = read_corla_file(discrepancies)
    except (OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    result = compare_with_corla(report, found, listed=listed)
    if as_json:
        typer.echo(json.dumps(result.to_dict(), indent=1, ensure_ascii=False))
    else:
        typer.echo(f"{source} ({note}), {report['county']} board {report['board']}; CORLA: {discrepancies}\n" + render_comparison(result, "observer", "CORLA"))
    raise typer.Exit(1 if result.differences else 0)


def register(app: typer.Typer) -> None:
    """Add the ``report`` command group to ``app``."""
    app.add_typer(report_app, name="report")
