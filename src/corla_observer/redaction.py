"""Check that a redacted CVR, as provided, leaves no small group of identifiable ballots.

Colorado's privacy rule for public records of votes (C.R.S.
24-72-205.5(4)(b)(III)) requires that no group of fewer than 10 ballots that
are identical in their contests and precinct coding be identifiable. A county
publishing its cast vote record (CVR) meets it by redacting: blanking the
votes of ballots in small groups, marking them, aggregating them into a
totals row, or removing or blanking the precinct column.

`check_redaction` judges the file exactly as the county provides it:

- **Rows.** Each data row is read and classified by `corla_observer.cvr`.
  A vote value is a vote cell that is neither blank nor a redaction marker
  (``*``, ``X`` or text beginning ``Redacted``). Two kinds of row are
  counted and excluded from grouping: rows with no vote values left
  (already redacted), and aggregate rows that combine several ballots
  (a vote value that is a whole number above 1, or a first cell such as
  ``AGGREGATED``, ``Total`` or ``Redacted and Consolidated 18 Ballots``).
  Every other row is a ballot and is grouped, including rows marked
  redacted (by a marker in their identification or other cells) that
  still carry vote values: their votes and their set of contests are
  visible, so they count. Those are counted as marked ballot rows.
- **Style.** A ballot's style is the set of contests on its card: the
  contests (named by the contest row, the CVR's second header row) with at
  least one vote value in the ballot's row, after removing whitespace and
  Excel's ``="..."`` wrapping. A 0 is a vote value, so a contest the voter
  left unmarked still counts as on the card. Vote values play no other
  part.
- **Precinct and ballot type.** The file carries precinct information if
  it has a precinct column (``PrecinctPortion`` or a similar name) and at
  least one ballot row has a non-blank value in it; likewise ballot type
  information, for a ``BallotType`` column (or a similar name). Ballots are
  grouped by (style, precinct value, ballot type value), using whichever of
  precinct and ballot type the file carries: by style alone if neither.
  Ballots with a blank value in a column that is used form groups of their
  own.
- **Verdict.** PASS when every group has at least `threshold` ballots (10
  by default), and FAIL when any group is smaller, or when the file cannot
  be read reliably (no CVR header, a malformed row, a row with cells beyond
  the header's width, or the check stopped by its limits).

The report (`RedactionReport`) holds counts and style, precinct and ballot
type information only, never votes: rows read, ballot rows grouped (and how
many of them are marked redacted), rows already redacted, aggregate rows,
styles and groups, whether precinct and ballot type information were
present, and for each small ("rare") group its size, precinct value, ballot
type value and a short description of its style: the number of contests, a
stable 12-hex-digit hash of the set of contest names (`style_hash`), and
optionally the names themselves. When the file carries ballot type
information, it also lists the contest patterns found under more than one
ballot type value (`SharedPattern`): the ballot type column distinguishes
them, so they are grouped separately, and the report gives each pattern's
ballot types and the ballot count under each.

`check_redaction` runs the analysis in a separate Python process with limits
on address space, CPU time and wall-clock time (`CheckLimits`), so that a
malformed or huge upload cannot hang or exhaust the server; a check stopped
by a limit is a FAIL. `analyze_redaction` runs the same analysis in the
calling process. The limits use the ``resource`` module, so they need a
Unix-like system.

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import signal
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corla_observer.cvr import CvrFormatError, CvrReader
from corla_observer.events import JSONValue

DEFAULT_THRESHOLD = 10

PASS = "PASS"
FAIL = "FAIL"

#: Rare groups kept in a report, smallest first (all are counted).
MAX_RARE_GROUPS = 1000
#: Rare groups included by `RedactionReport.to_dict` by default, as in events.
EVENT_RARE_GROUPS = 50
#: Contest patterns shared by several ballot types kept in a report (all are counted).
MAX_SHARED_PATTERNS = 100
#: Ballot types kept for one shared pattern (all are counted).
MAX_SHARED_TYPES = 50
#: Longest precinct value or problem text kept in a report.
_MAX_TEXT = 128
_STYLE_HASH_DIGITS = 12

#: A grouping key: the style (contest set), and the precinct and ballot type values or None.
Group = tuple[frozenset[str], str | None, str | None]


def style_hash(contests: Iterable[str]) -> str:
    """A short, stable name for a set of contests.

    The first 12 hex digits of the SHA-256 of the contest names, sorted by
    code point and joined by line feeds, in UTF-8. The same set of contests
    gets the same hash in any file that names them identically.
    """
    joined = "\n".join(sorted(contests)).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:_STYLE_HASH_DIGITS]


def _short(text: str) -> str:
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 3] + "..."


@dataclass(frozen=True)
class RareGroup:
    """A group of ballots below the threshold: one style, and one precinct and ballot type value where used."""

    size: int
    #: The group's precinct value, or None when precinct is not used or for a blank value.
    precinct: str | None
    #: The group's ballot type value, or None when ballot type is not used or for a blank value.
    ballot_type: str | None
    #: Number of contests on the style's cards.
    contests: int
    style_hash: str
    #: The contest names, sorted, when asked for.
    contest_names: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, JSONValue]:
        result: dict[str, JSONValue] = {
            "size": self.size,
            "precinct": self.precinct,
            "ballot_type": self.ballot_type,
            "contests": self.contests,
            "style_hash": self.style_hash,
        }
        if self.contest_names is not None:
            result["contest_names"] = list(self.contest_names)
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RareGroup:
        names = data.get("contest_names")
        return cls(
            size=int(data["size"]),
            precinct=None if data.get("precinct") is None else str(data["precinct"]),
            ballot_type=None if data.get("ballot_type") is None else str(data["ballot_type"]),
            contests=int(data["contests"]),
            style_hash=str(data["style_hash"]),
            contest_names=None if names is None else tuple(str(n) for n in names),
        )


@dataclass(frozen=True)
class SharedPattern:
    """A contest pattern (style) found under more than one ballot type value.

    The ballot type column distinguishes the ballots, so each ballot type's
    share is grouped separately; this records which ballot types share it.
    """

    style_hash: str
    #: Number of contests in the pattern.
    contests: int
    #: Ballots with this pattern, under every ballot type.
    ballots: int
    #: Number of distinct ballot type values (a blank value counts as one).
    ballot_type_count: int
    #: ``(ballot type value or None for blank, ballots)``, sorted by value, up to `MAX_SHARED_TYPES`.
    ballot_types: tuple[tuple[str | None, int], ...]

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "style_hash": self.style_hash,
            "contests": self.contests,
            "ballots": self.ballots,
            "ballot_type_count": self.ballot_type_count,
            "ballot_types": [[value, count] for value, count in self.ballot_types],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SharedPattern:
        return cls(
            style_hash=str(data["style_hash"]),
            contests=int(data["contests"]),
            ballots=int(data["ballots"]),
            ballot_type_count=int(data["ballot_type_count"]),
            ballot_types=tuple(
                (None if value is None else str(value), int(count)) for value, count in data["ballot_types"]
            ),
        )


@dataclass(frozen=True)
class RedactionReport:
    """The result of a redaction check: counts and style, precinct and ballot type information, never votes."""

    verdict: str
    threshold: int
    #: Non-blank data rows read after the header.
    rows_read: int = 0
    #: Rows grouped as ballots.
    ballot_rows: int = 0
    #: Of those, rows marked redacted that still carry vote values.
    marked_ballot_rows: int = 0
    #: Rows with no vote values left.
    already_redacted_rows: int = 0
    #: Rows that combine several ballots.
    aggregate_rows: int = 0
    #: The precinct column's name as written, if the file has one.
    precinct_column: str | None = None
    #: Whether any ballot row has a non-blank precinct value.
    precinct_present: bool = False
    #: The ballot type column's name as written, if the file has one.
    ballot_type_column: str | None = None
    #: Whether any ballot row has a non-blank ballot type value.
    ballot_type_present: bool = False
    #: "style", "style+precinct", "style+ballot_type" or "style+precinct+ballot_type".
    grouped_by: str = "style"
    styles: int = 0
    groups: int = 0
    rare_group_count: int = 0
    rare_ballots: int = 0
    #: Marked ballot rows in rare groups, and the number of rare groups holding any.
    marked_rows_in_rare_groups: int = 0
    rare_groups_with_marked_rows: int = 0
    #: ``(size, number of rare groups of that size)``, for every rare size.
    rare_group_sizes: tuple[tuple[int, int], ...] = ()
    #: The smallest rare groups, up to `MAX_RARE_GROUPS`.
    rare_groups: tuple[RareGroup, ...] = ()
    #: Contest patterns found under more than one ballot type value, and the first `MAX_SHARED_PATTERNS` of them.
    shared_pattern_count: int = 0
    shared_patterns: tuple[SharedPattern, ...] = ()
    #: Reasons the file could not be checked reliably; any problem makes the verdict FAIL.
    problems: tuple[str, ...] = ()
    #: Things worth a look that do not change the verdict.
    warnings: tuple[str, ...] = ()
    #: How the file was read: encoding, delimiter, header row, contest and vote column counts.
    format: dict[str, JSONValue] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict == PASS

    @classmethod
    def failed(cls, problem: str, threshold: int = DEFAULT_THRESHOLD) -> RedactionReport:
        """A FAIL report for a file that could not be checked."""
        return cls(verdict=FAIL, threshold=threshold, problems=(_short(problem),))

    def to_dict(self, max_groups: int | None = EVENT_RARE_GROUPS) -> dict[str, JSONValue]:
        """The report as JSON values, listing at most ``max_groups`` rare groups (None: all kept)."""
        groups = self.rare_groups if max_groups is None else self.rare_groups[:max_groups]
        return {
            "verdict": self.verdict,
            "threshold": self.threshold,
            "rows_read": self.rows_read,
            "ballot_rows": self.ballot_rows,
            "marked_ballot_rows": self.marked_ballot_rows,
            "already_redacted_rows": self.already_redacted_rows,
            "aggregate_rows": self.aggregate_rows,
            "precinct_column": self.precinct_column,
            "precinct_present": self.precinct_present,
            "ballot_type_column": self.ballot_type_column,
            "ballot_type_present": self.ballot_type_present,
            "grouped_by": self.grouped_by,
            "styles": self.styles,
            "groups": self.groups,
            "rare_group_count": self.rare_group_count,
            "rare_ballots": self.rare_ballots,
            "marked_rows_in_rare_groups": self.marked_rows_in_rare_groups,
            "rare_groups_with_marked_rows": self.rare_groups_with_marked_rows,
            "rare_group_sizes": [[size, count] for size, count in self.rare_group_sizes],
            "rare_groups": [group.to_dict() for group in groups],
            "rare_groups_listed": len(groups),
            "shared_pattern_count": self.shared_pattern_count,
            "shared_patterns": [pattern.to_dict() for pattern in self.shared_patterns],
            "problems": list(self.problems),
            "warnings": list(self.warnings),
            "format": dict(self.format),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RedactionReport:
        verdict = data["verdict"]
        if verdict not in (PASS, FAIL):
            raise ValueError(f"unknown verdict {verdict!r}")
        return cls(
            verdict=verdict,
            threshold=int(data["threshold"]),
            rows_read=int(data["rows_read"]),
            ballot_rows=int(data["ballot_rows"]),
            marked_ballot_rows=int(data["marked_ballot_rows"]),
            already_redacted_rows=int(data["already_redacted_rows"]),
            aggregate_rows=int(data["aggregate_rows"]),
            precinct_column=data.get("precinct_column"),
            precinct_present=bool(data["precinct_present"]),
            ballot_type_column=data.get("ballot_type_column"),
            ballot_type_present=bool(data["ballot_type_present"]),
            grouped_by=str(data["grouped_by"]),
            styles=int(data["styles"]),
            groups=int(data["groups"]),
            rare_group_count=int(data["rare_group_count"]),
            rare_ballots=int(data["rare_ballots"]),
            marked_rows_in_rare_groups=int(data["marked_rows_in_rare_groups"]),
            rare_groups_with_marked_rows=int(data["rare_groups_with_marked_rows"]),
            rare_group_sizes=tuple((int(s), int(n)) for s, n in data["rare_group_sizes"]),
            rare_groups=tuple(RareGroup.from_dict(g) for g in data["rare_groups"]),
            # Absent from reports recorded before shared patterns were listed.
            shared_pattern_count=int(data.get("shared_pattern_count", 0)),
            shared_patterns=tuple(SharedPattern.from_dict(p) for p in data.get("shared_patterns", ())),
            problems=tuple(str(p) for p in data["problems"]),
            warnings=tuple(str(w) for w in data["warnings"]),
            format=dict(data["format"]),
        )


# ---------------------------------------------------------------------------
# The analysis


def analyze_redaction(
    path: Path | str,
    threshold: int = DEFAULT_THRESHOLD,
    *,
    include_contest_names: bool = False,
) -> RedactionReport:
    """Check a CVR file in this process; see the module documentation and `check_redaction`.

    Never raises for a malformed file: the problem is reported and the verdict is FAIL.
    """
    if threshold < 1:
        raise ValueError(f"threshold must be at least 1, got {threshold}")
    try:
        reader = CvrReader(path)
    except CvrFormatError as exc:
        return RedactionReport.failed(f"not a readable CVR: {exc}", threshold)
    except (OSError, UnicodeError, csv.Error) as exc:
        return RedactionReport.failed(f"could not read the file: {exc}", threshold)
    header = reader.header
    rows_read = redacted = aggregate = 0
    extra_rows = 0
    first_extra: int | None = None
    # Key: (contest set, precinct value, ballot type value). Value: ballot count,
    # and the count of those marked redacted.
    by_group: Counter[Group] = Counter()
    marked: Counter[Group] = Counter()
    precinct_values = type_values = False
    problems: list[str] = []
    try:
        for row in reader.rows():
            rows_read += 1
            if row.extra_cells:
                extra_rows += 1
                first_extra = first_extra or row.record
            if row.status == "redacted":
                redacted += 1
                continue
            if row.status == "aggregate":
                aggregate += 1
                continue
            precinct_values = precinct_values or row.precinct is not None
            type_values = type_values or row.ballot_type is not None
            key = (row.contests, row.precinct, row.ballot_type)
            by_group[key] += 1
            if row.marked:
                marked[key] += 1
    except (csv.Error, UnicodeError, OSError) as exc:
        problems.append(_short(f"the file could not be read past data row {rows_read}: {exc}"))
    if extra_rows:
        problems.append(
            f"{extra_rows} row(s) have non-blank cells beyond the header's {header.width} "
            f"columns (first at CSV record {first_extra})"
        )

    # A column with no values on ballot rows is None in every key already, so
    # grouping by the full key groups by whichever of precinct and ballot type
    # the file carries.
    grouped_by = "style" + ("+precinct" if precinct_values else "") + ("+ballot_type" if type_values else "")
    styles = {contests for contests, _, _ in by_group}
    rare = sorted(
        ((size, key) for key, size in by_group.items() if size < threshold),
        key=lambda item: (item[0], item[1][1] or "", item[1][2] or "", style_hash(item[1][0])),
    )
    sizes = Counter(size for size, _ in rare)
    warnings: list[str] = []
    if header.unnamed_vote_columns:
        warnings.append(
            f"{header.unnamed_vote_columns} vote column(s) have no contest name; "
            "each is treated as a contest of its own"
        )
    rare_groups = tuple(
        RareGroup(
            size=size,
            precinct=None if precinct is None else _short(precinct),
            ballot_type=None if ballot_type is None else _short(ballot_type),
            contests=len(contests),
            style_hash=style_hash(contests),
            contest_names=tuple(sorted(contests)) if include_contest_names else None,
        )
        for size, (contests, precinct, ballot_type) in rare[:MAX_RARE_GROUPS]
    )
    shared = _shared_patterns(by_group) if type_values else []
    verdict = PASS if not rare and not problems else FAIL
    return RedactionReport(
        verdict=verdict,
        threshold=threshold,
        rows_read=rows_read,
        ballot_rows=sum(by_group.values()),
        marked_ballot_rows=sum(marked.values()),
        already_redacted_rows=redacted,
        aggregate_rows=aggregate,
        precinct_column=header.precinct_column,
        precinct_present=precinct_values,
        ballot_type_column=header.ballot_type_column,
        ballot_type_present=type_values,
        grouped_by=grouped_by,
        styles=len(styles),
        groups=len(by_group),
        rare_group_count=len(rare),
        rare_ballots=sum(size for size, _ in rare),
        marked_rows_in_rare_groups=sum(marked[key] for _, key in rare),
        rare_groups_with_marked_rows=sum(1 for _, key in rare if marked[key]),
        rare_group_sizes=tuple(sorted(sizes.items())),
        rare_groups=rare_groups,
        shared_pattern_count=len(shared),
        shared_patterns=tuple(shared[:MAX_SHARED_PATTERNS]),
        problems=tuple(problems),
        warnings=tuple(warnings),
        format={
            "encoding": header.encoding,
            "delimiter": header.delimiter,
            "header_row": header.header_row,
            "election": None if header.election is None else _short(header.election),
            "version": None if header.version is None else _short(header.version),
            "columns": header.width,
            "vote_columns": header.width - header.vote_start,
            "contests": len(header.contests),
        },
    )


def _type_order(value: str | None) -> tuple[bool, str]:
    return (value is None, value or "")


def _shared_patterns(by_group: Mapping[Group, int]) -> list[SharedPattern]:
    """The contest patterns found under more than one ballot type value, ordered by their ballot types."""
    by_style: dict[frozenset[str], Counter[str | None]] = {}
    for (contests, _, ballot_type), size in by_group.items():
        by_style.setdefault(contests, Counter())[ballot_type] += size
    patterns = []
    for contests, types in by_style.items():
        if len(types) < 2:
            continue
        ordered = sorted(types.items(), key=lambda item: _type_order(item[0]))
        patterns.append(
            SharedPattern(
                style_hash=style_hash(contests),
                contests=len(contests),
                ballots=sum(types.values()),
                ballot_type_count=len(types),
                ballot_types=tuple(
                    (None if value is None else _short(value), count) for value, count in ordered[:MAX_SHARED_TYPES]
                ),
            )
        )
    patterns.sort(key=lambda p: ([_type_order(v) for v, _ in p.ballot_types], p.style_hash))
    return patterns


# ---------------------------------------------------------------------------
# Running the analysis in a limited worker process


@dataclass(frozen=True)
class CheckLimits:
    """Limits on the worker process that checks one file."""

    #: Address-space limit (RLIMIT_AS), in bytes.
    max_memory_bytes: int = 1024 * 1024 * 1024
    #: CPU-time limit (RLIMIT_CPU), in whole seconds.
    max_cpu_seconds: int = 600
    #: Wall-clock limit, after which the worker is killed, in seconds.
    timeout_seconds: float = 900.0


def _worker_command(path: Path, threshold: int, include_contest_names: bool, limits: CheckLimits) -> list[str]:
    command = [
        sys.executable,
        "-I",  # isolated: ignore PYTHON* variables, the user site and the current directory
        "-B",  # write no bytecode files
        "-m",
        "corla_observer.redaction",
        "--worker",
        "--threshold",
        str(threshold),
        "--max-memory",
        str(limits.max_memory_bytes),
        "--max-cpu",
        str(limits.max_cpu_seconds),
    ]
    if include_contest_names:
        command.append("--names")
    command.append(str(path))
    return command


def check_redaction(
    path: Path | str,
    threshold: int = DEFAULT_THRESHOLD,
    *,
    include_contest_names: bool = False,
    limits: CheckLimits | None = None,
) -> RedactionReport:
    """Check a CVR file, as provided, in a separate process with time and memory limits.

    See the module documentation for the rule. A file the worker cannot
    finish within ``limits`` (a `CheckLimits`, by default 1 GiB of address
    space, 600 CPU seconds and 900 seconds in all), or that makes it fail,
    gets a FAIL report saying why. Raises only for a bad ``threshold``.
    """
    if threshold < 1:
        raise ValueError(f"threshold must be at least 1, got {threshold}")
    limits = limits or CheckLimits()
    command = _worker_command(Path(path).resolve(), threshold, include_contest_names, limits)
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=limits.timeout_seconds,
            cwd="/",
        )
    except subprocess.TimeoutExpired:
        return RedactionReport.failed(
            f"check stopped: it took longer than {limits.timeout_seconds:g} seconds", threshold
        )
    stderr = result.stderr.decode("utf-8", "replace")
    if result.returncode != 0:
        if "MemoryError" in stderr:
            reason = f"check stopped: it needed more than {limits.max_memory_bytes} bytes of memory"
        elif result.returncode in (-signal.SIGXCPU, -signal.SIGKILL):
            reason = f"check stopped: it used more than {limits.max_cpu_seconds} CPU seconds"
        else:
            last = stderr.strip().splitlines()[-1:] or ["no message"]
            reason = f"check failed (exit status {result.returncode}): {last[0]}"
        return RedactionReport.failed(reason, threshold)
    try:
        return RedactionReport.from_dict(json.loads(result.stdout))
    except (ValueError, KeyError, TypeError) as exc:
        return RedactionReport.failed(f"check failed: unreadable worker output ({exc})", threshold)


def _apply_limits(max_memory: int, max_cpu: int) -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (max_memory, max_memory))
    resource.setrlimit(resource.RLIMIT_CPU, (max_cpu, max_cpu + 5))
    # The worker writes nothing but its report, to a pipe.
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _worker_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m corla_observer.redaction")
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--names", action="store_true")
    parser.add_argument("--max-memory", type=int, required=True)
    parser.add_argument("--max-cpu", type=int, required=True)
    parser.add_argument("path")
    args = parser.parse_args(argv)
    _apply_limits(args.max_memory, args.max_cpu)
    report = analyze_redaction(args.path, args.threshold, include_contest_names=args.names)
    sys.stdout.write(json.dumps(report.to_dict(max_groups=None), ensure_ascii=False))
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# Presentation


def _column(name: str | None, present: bool) -> str:
    if name is None:
        return "none"
    return f"{name!r} ({'with values' if present else 'blank'})"


def _type_name(value: str | None) -> str:
    return "(blank)" if value is None else value


def format_shared_pattern(pattern: SharedPattern) -> str:
    """One shared pattern as ``[03, 11, 11 & PO-CC] (N ballots: a, b, c)``, with its style hash."""
    more = pattern.ballot_type_count - len(pattern.ballot_types)
    names = ", ".join(_type_name(value) for value, _ in pattern.ballot_types) + (f", and {more} more" if more else "")
    counts = ", ".join(str(count) for _, count in pattern.ballot_types) + (", ..." if more else "")
    return (
        f"[{names}] ({pattern.ballots} ballots: {counts}), "
        f"style {pattern.style_hash} ({pattern.contests} contests)"
    )


def format_report(report: RedactionReport, max_groups: int | None = 20, max_shared: int = 20) -> str:
    """The report as readable text: counts, styles, precincts and ballot types only.

    Lists at most ``max_groups`` rare groups (None: all kept) and ``max_shared`` shared patterns.
    """
    lines = [
        f"{report.verdict}: {report.rare_group_count} group(s) below the threshold of "
        f"{report.threshold} ballots, grouped by {report.grouped_by}",
        f"  rows read {report.rows_read}: {report.ballot_rows} ballots grouped "
        f"({report.marked_ballot_rows} of them marked redacted but with votes), "
        f"{report.already_redacted_rows} already redacted, {report.aggregate_rows} aggregate",
        f"  {report.styles} contest patterns (sets of contests on a card; a card with a contest blanked out has its own), "
        f"{report.groups} groups (pattern, precinct and ballot type); precinct column "
        f"{_column(report.precinct_column, report.precinct_present)}; ballot type column "
        f"{_column(report.ballot_type_column, report.ballot_type_present)}",
    ]
    if report.rare_group_count:
        sizes = ", ".join(
            f"{size}" + (f" x{count}" if count > 1 else "") for size, count in report.rare_group_sizes
        )
        lines.append(f"  rare group sizes: {sizes} ({report.rare_ballots} ballots)")
        if report.marked_rows_in_rare_groups:
            lines.append(
                f"  {report.marked_rows_in_rare_groups} of those ballots, in "
                f"{report.rare_groups_with_marked_rows} group(s), are rows marked redacted that keep their votes"
            )
        shown = report.rare_groups if max_groups is None else report.rare_groups[:max_groups]
        for group in shown:
            where = ""
            if report.precinct_present:
                where += f"precinct {group.precinct!r}, "
            if report.ballot_type_present:
                where += f"ballot type {group.ballot_type!r}, "
            lines.append(
                f"    {group.size} ballot(s): {where}style {group.style_hash} "
                f"({group.contests} contests)"
            )
            if group.contest_names is not None:
                lines.extend(f"      {name}" for name in group.contest_names)
        if len(shown) < report.rare_group_count:
            lines.append(f"    ... and {report.rare_group_count - len(shown)} more")
    if report.shared_pattern_count:
        lines.append(
            f"  {report.shared_pattern_count} contest pattern(s) shared by several ballot types "
            "(the ballot type column distinguishes them, so they are grouped separately):"
        )
        shared = report.shared_patterns[:max_shared]
        lines.extend(f"    {format_shared_pattern(pattern)}" for pattern in shared)
        if len(shared) < report.shared_pattern_count:
            lines.append(f"    ... and {report.shared_pattern_count - len(shared)} more")
    lines.extend(f"  problem: {problem}" for problem in report.problems)
    lines.extend(f"  warning: {warning}" for warning in report.warnings)
    return "\n".join(lines)


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(_worker_main(sys.argv[1:]))
