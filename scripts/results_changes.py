#!/usr/bin/env python3
"""Show how Clarity's published results changed from one saved version to the next, by county.

Written 2026-11-25 to answer: did any county's vote counts change after the 2026 general's risk-limiting
audit (16 to 19 November), and how big were the changes? It reads the results files the relay saved
(`scripts/relay_results.sh`, default directory ~/.local/state/corla-observer-relay/HOST/files, named
`state-VERSION-detailxml.zip` and `County-VERSION-summary.json`), and for each scope compares each
version with the one before it:

- For the state's detail files, each county's votes in every contest the state reports, and the
  county's ballots cast.
- For a county's own summary files, each contest's total votes (county, municipal and district
  contests included).

For each change it prints when Clarity generated the newer file (MST), the county, how many contests
changed, the total votes added or removed, the change in ballots cast (also as a share of the
county's ballots cast, where the state's file gives them), and the largest change in one contest.
`--from VERSION` compares that one version with the newest instead of walking every step.

    .venv/bin/python scripts/results_changes.py [--dir DIR] [--scope state|COUNTY] [--from VERSION]
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from corla_observer.enr import Results, read_results

DEFAULT_DIR = Path.home() / ".local/state/corla-observer-relay/corla-observer.sprite-app/files"
NAME = re.compile(r"^(?P<scope>.+)-(?P<version>\d+)-(?:detailxml\.zip|summary\.json)$")
MST = timezone(timedelta(hours=-7))


def saved_versions(directory: Path) -> dict[str, list[tuple[int, Path]]]:
    """Each scope ("state", or a county as named in the file) and its saved versions, oldest first."""
    found: dict[str, list[tuple[int, Path]]] = {}
    for path in directory.iterdir():
        m = NAME.match(path.name)
        if m:
            scope = m["scope"].replace("_", " ")
            found.setdefault(scope, []).append((int(m["version"]), path))
    return {scope: sorted(items) for scope, items in found.items()}


def votes_by_county(results: Results, scope: str) -> dict[str, dict[str, int]]:
    """County -> contest -> total votes (all choices). A county's own file is one county, its totals."""
    out: dict[str, dict[str, int]] = {}
    if scope == "state":
        for contest in results.contests:
            for county, choices in contest.by_area.items():
                out.setdefault(county, {})[contest.name] = sum(choices.values())
        for county, cast in results.ballots_cast.items():
            out.setdefault(county, {})["(ballots cast)"] = cast
    else:
        out[scope] = {c.name: sum(c.totals.values()) for c in results.contests}
    return out


def when(results: Results) -> str:
    """When Clarity generated the file, in MST, if it says."""
    if not results.generated:
        return "time not given"
    t = datetime.fromisoformat(results.generated.replace("Z", "+00:00"))
    return t.astimezone(MST).strftime("%Y-%m-%d %H:%M MST")


def compare(old: dict[str, dict[str, int]], new: dict[str, dict[str, int]]) -> list[tuple[str, int, int, str, int]]:
    """Per county with any change: (county, contests changed, net votes, largest contest, its change)."""
    rows = []
    for county in sorted(set(old) | set(new)):
        a, b = old.get(county, {}), new.get(county, {})
        deltas = {k: b.get(k, 0) - a.get(k, 0) for k in set(a) | set(b) if k != "(ballots cast)"}
        deltas = {k: d for k, d in deltas.items() if d}
        cast = b.get("(ballots cast)", 0) - a.get("(ballots cast)", 0)
        if not deltas and not cast:
            continue
        big = max(deltas, key=lambda k: abs(deltas[k])) if deltas else ""
        rows.append((county, len(deltas), sum(deltas.values()), big, deltas.get(big, 0), cast,
                     b.get("(ballots cast)", 0)))
    return rows


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    p.add_argument("--scope", help='"state" or a county name; default every scope saved')
    p.add_argument("--from", dest="from_version", type=int, help="compare this version with the newest")
    p.add_argument("--county", action="append", help="only report these counties (repeatable)")
    args = p.parse_args(argv)

    for scope, items in saved_versions(args.dir).items():
        if args.scope and scope != args.scope:
            continue
        if args.from_version is not None:
            items = [i for i in items if i[0] == args.from_version] + [items[-1]]
            if len(items) < 2:
                continue
        print(f"== {scope}: {len(items)} versions")
        prev = None
        for version, path in items:
            results = read_results(path.read_bytes(), "state" if scope == "state" else scope)
            current = votes_by_county(results, scope)
            if prev is not None:
                rows = [r for r in compare(prev[1], current) if not args.county or r[0] in args.county]
                for county, n, net, big, bigd, cast, total in rows:
                    share = f" ({100 * cast / total:+.3f}% of {total:,})" if total else ""
                    print(f"  {prev[0]} -> {version} ({when(results)}) {county}: {n} contests changed, "
                          f"net {net:+d} votes, ballots cast {cast:+d}{share}; largest {bigd:+d} in {big}")
            prev = (version, current)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
